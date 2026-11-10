#!/usr/bin/env python3
"""Ollama forwarder for sandboxed mates workers.

Two halves joined by a unix socket:

  out  (outside the sandbox, as wai): unix socket -> Ollama TCP. Allows only
       POST /v1/messages[/count_tokens] for one model, and logs one JSONL line
       per request (requested vs answering model, token usage, status). This
       log is the audit source for `answered_by` and `worker_tokens`.
  in   (inside the sandbox, no network): 127.0.0.1:11434 TCP -> unix socket,
       a plain byte pipe.

One HTTP request per connection: `out` sends `Connection: close` upstream and
closes the client side when the response ends.
"""
import argparse
import asyncio
import grp
import json
import os
import time

ALLOWED_PATHS = {"/v1/messages", "/v1/messages/count_tokens"}


def _dechunk(body: bytes) -> bytes:
    out, i = bytearray(), 0
    while i < len(body):
        j = body.find(b"\r\n", i)
        if j < 0:
            break
        try:
            size = int(body[i:j].split(b";")[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        out += body[j + 2 : j + 2 + size]
        i = j + 2 + size + 2
    return bytes(out)


def parse_usage(data: bytes) -> dict:
    """Usage from an Anthropic Messages response, JSON or SSE stream."""
    res = {"response_model": None, "input_tokens": 0, "output_tokens": 0}
    text = data.decode("utf-8", "replace").strip()
    if text.startswith("{"):
        try:
            d = json.loads(text)
        except ValueError:
            return res
        u = d.get("usage") or {}
        return {"response_model": d.get("model"), "input_tokens": int(u.get("input_tokens", 0)),
                "output_tokens": int(u.get("output_tokens", 0))}
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except ValueError:
            continue
        if ev.get("type") == "message_start":
            m = ev.get("message") or {}
            res["response_model"] = m.get("model")
            u = m.get("usage") or {}
            res["input_tokens"] = int(u.get("input_tokens", 0))
            res["output_tokens"] = int(u.get("output_tokens", 0))
        elif ev.get("type") == "message_delta":
            u = ev.get("usage") or {}
            if "output_tokens" in u:
                res["output_tokens"] = int(u["output_tokens"])
            if u.get("input_tokens"):
                res["input_tokens"] = int(u["input_tokens"])
    return res


def _log(path: str, rec: dict) -> None:
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **rec}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(rec) + "\n").encode())
    finally:
        os.close(fd)


async def _read_request(reader):
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    method, path, _ = lines[0].split(" ", 2)
    headers = []
    for l in lines[1:]:
        if ":" in l:
            k, v = l.split(":", 1)
            headers.append((k.strip(), v.strip()))
    n = next((int(v) for k, v in headers if k.lower() == "content-length"), 0)
    body = await reader.readexactly(n) if n else b""
    return method, path, headers, body


async def _deny(writer, log, path, model, why):
    msg = json.dumps({"error": why}).encode()
    writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\nContent-Length: "
                 + str(len(msg)).encode() + b"\r\nConnection: close\r\n\r\n" + msg)
    await writer.drain()
    _log(log, {"path": path, "requested_model": model, "response_model": None,
               "input_tokens": 0, "output_tokens": 0, "status": 403, "why": why})


async def _handle_out(reader, writer, upstream, log, model):
    try:
        method, path, headers, body = await _read_request(reader)
        req_model = None
        try:
            req_model = json.loads(body).get("model") if body else None
        except ValueError:
            pass
        bare = path.split("?", 1)[0]
        if method != "POST" or bare not in ALLOWED_PATHS:
            return await _deny(writer, log, path, req_model, "path not allowed")
        if req_model != model:
            return await _deny(writer, log, path, req_model, "model not allowed")
        ur, uw = await asyncio.open_connection(*upstream)
        keep = [(k, v) for k, v in headers
                if k.lower() not in ("connection", "accept-encoding", "host", "keep-alive")]
        hdr = f"{method} {path} HTTP/1.1\r\nHost: {upstream[0]}:{upstream[1]}\r\nConnection: close\r\n"
        hdr += "".join(f"{k}: {v}\r\n" for k, v in keep) + "\r\n"
        uw.write(hdr.encode("latin-1") + body)
        await uw.drain()
        buf = bytearray()
        while True:
            chunk = await ur.read(65536)
            if not chunk:
                break
            buf += chunk
            writer.write(chunk)
            await writer.drain()
        uw.close()
        head, _, rbody = bytes(buf).partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1]) if head.startswith(b"HTTP/") else 0
        if b"transfer-encoding: chunked" in head.lower():
            rbody = _dechunk(rbody)
        _log(log, {"path": path, "requested_model": req_model, "status": status, **parse_usage(rbody)})
    except (asyncio.IncompleteReadError, ConnectionError, ValueError) as e:
        _log(log, {"path": None, "requested_model": None, "response_model": None,
                   "input_tokens": 0, "output_tokens": 0, "status": 0, "why": repr(e)})
    finally:
        writer.close()


async def _pipe(r, w):
    try:
        while True:
            b = await r.read(65536)
            if not b:
                break
            w.write(b)
            await w.drain()
    except ConnectionError:
        pass
    finally:
        w.close()


async def _handle_in(reader, writer, sock):
    ur, uw = await asyncio.open_unix_connection(sock)
    await asyncio.gather(_pipe(reader, uw), _pipe(ur, writer))


async def serve_out(sock: str, upstream: tuple, log: str, model: str, group: str | None = None):
    if os.path.exists(sock):
        os.unlink(sock)
    srv = await asyncio.start_unix_server(lambda r, w: _handle_out(r, w, upstream, log, model), path=sock)
    os.chmod(sock, 0o660)
    if group:
        os.chown(sock, -1, grp.getgrnam(group).gr_gid)
    async with srv:
        await srv.serve_forever()


async def serve_in(sock: str, listen: tuple):
    srv = await asyncio.start_server(lambda r, w: _handle_in(r, w, sock), *listen)
    async with srv:
        await srv.serve_forever()


def _hostport(s: str) -> tuple:
    h, p = s.rsplit(":", 1)
    return h, int(p)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    o = sub.add_parser("out")
    o.add_argument("--sock", required=True)
    o.add_argument("--upstream", default="127.0.0.1:11435")
    o.add_argument("--log", required=True)
    o.add_argument("--model", required=True)
    o.add_argument("--group", default="emetcode")
    i = sub.add_parser("in")
    i.add_argument("--sock", required=True)
    i.add_argument("--listen", default="127.0.0.1:11434")
    a = ap.parse_args()
    if a.mode == "out":
        asyncio.run(serve_out(a.sock, _hostport(a.upstream), a.log, a.model, a.group))
    else:
        asyncio.run(serve_in(a.sock, _hostport(a.listen)))


if __name__ == "__main__":
    main()
