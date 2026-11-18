"""qwen-relay egress: the only way out of a runtime build. CONNECT to port 443 of a public
address only; never loopback, the LAN, the WSL host, or a local service such as emet's
MCP server on 127.0.0.1."""
import importlib.machinery
import importlib.util
import pathlib
import socket
import subprocess
import sys
import time

import pytest

RELAY = pathlib.Path(__file__).resolve().parent.parent / "bin" / "qwen-relay"


def _load():
    loader = importlib.machinery.SourceFileLoader("qwen_relay", str(RELAY))
    spec = importlib.util.spec_from_loader("qwen_relay", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


relay = _load()


@pytest.mark.parametrize("ip", ["8.8.8.8", "151.101.0.223", "2606:4700:4700::1111"])
def test_public_addresses_allowed(ip):
    assert relay.public_addr(ip)


@pytest.mark.parametrize("ip", ["127.0.0.1", "127.0.0.53", "10.255.255.254", "192.168.1.5", "172.17.0.1",
                                "169.254.169.254", "100.64.0.1", "0.0.0.0", "224.0.0.1", "::1",
                                "fe80::1", "fd00::1", "::ffff:127.0.0.1", "::", "::127.0.0.1", "64:ff9b::7f00:1",
                                "2002:7f00:1::1", "64:ff9b::a00:1"])
def test_local_and_private_addresses_refused(ip):
    assert not relay.public_addr(ip)


def test_parse_connect():
    assert relay.parse_connect(b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org:443\r\n\r\n") == ("pypi.org", 443)
    assert relay.parse_connect(b"CONNECT [2606:4700::1]:443 HTTP/1.1\r\n\r\n") == ("2606:4700::1", 443)


@pytest.mark.parametrize("head", [b"GET http://pypi.org/ HTTP/1.1\r\n\r\n", b"CONNECT pypi.org HTTP/1.1\r\n\r\n",
                                  b"CONNECT pypi.org:443\r\n\r\n", b"CONNECT bad/host:443 HTTP/1.1\r\n\r\n",
                                  b"CONNECT :443 HTTP/1.1\r\n\r\n"])
def test_parse_connect_refuses_other_requests(head):
    with pytest.raises(ValueError):
        relay.parse_connect(head)


def test_pick_public_drops_local_answers():
    infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
             (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
    assert relay.pick_public(infos) == [(socket.AF_INET, ("8.8.8.8", 443))]


@pytest.fixture
def egress(private_dir):
    sock = private_dir / "egress.sock"
    p = subprocess.Popen([sys.executable, "-I", str(RELAY), "egress", "--sock", str(sock)],
                         stderr=subprocess.PIPE, text=True)
    for _ in range(50):
        if sock.exists():
            break
        time.sleep(0.05)
    yield sock, p
    p.kill()
    p.wait()


def _ask(sock, head):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(5)
    s.connect(str(sock))
    s.sendall(head)
    reply = s.recv(4096)
    s.close()
    return reply


@pytest.mark.parametrize("target", [b"127.0.0.1:443", b"localhost:443", b"10.255.255.254:443", b"[::1]:443"])
def test_egress_refuses_local_targets(egress, target):
    sock, _ = egress
    assert _ask(sock, b"CONNECT " + target + b" HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")


@pytest.mark.parametrize("host", ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org",
                                  "cdn.playwright.dev", "playwright.download.prss.microsoft.com"])
def test_registries_allowed(host):
    assert relay.allowed_host(host)


@pytest.mark.parametrize("host", ["github.com", "evil.example", "pypi.org.evil.example", "8.8.8.8", "xpypi.org",
                                  "playwright.dev", "evil.cdn.playwright.dev", "download.prss.microsoft.com"])
def test_other_hosts_refused(host):
    assert not relay.allowed_host(host)


def test_egress_refuses_hosts_off_the_list(egress):
    sock, p = egress
    assert _ask(sock, b"CONNECT github.com:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
    p.kill()
    assert "refused github.com:443 (not a package registry)" in p.stderr.read()


def test_egress_refuses_other_ports_and_plain_http(egress):
    sock, _ = egress
    assert _ask(sock, b"CONNECT example.com:80 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
    assert _ask(sock, b"GET http://example.com/ HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")


def test_egress_logs_each_refusal(egress):
    sock, p = egress
    _ask(sock, b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n")
    p.kill()
    assert "refused 127.0.0.1:443" in p.stderr.read()


# --- the model relay (`out`): the sandbox reaches the owner's Ollama through it, so it must
# pass only the chat API. Ollama's other endpoints act as the owner with the network: pull
# fetches from any registry host a model name names (data out), delete/create change models.
# One fully read request per connection, Connection: close upstream; a second request in the
# same connection never reaches the model server.

import socketserver  # noqa: E402
import threading  # noqa: E402


class _Upstream(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@pytest.fixture
def model_relay(private_dir):
    got = []

    class H(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(2)
            data = b""
            try:
                while chunk := self.request.recv(65536):
                    data += chunk
                    if b"\r\n\r\n" in data:
                        head, _, body = data.partition(b"\r\n\r\n")
                        n = int(next((l.split(b":")[1] for l in head.split(b"\r\n")
                                      if l.lower().startswith(b"content-length:")), b"0"))
                        if len(body) >= n:
                            break
            except OSError:
                pass
            got.append(data)
            self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")

    up = _Upstream(("127.0.0.1", 0), H)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    sock = private_dir / "model.sock"
    p = subprocess.Popen([sys.executable, "-I", str(RELAY), "out", "--sock", str(sock),
                          "--to", f"127.0.0.1:{up.server_address[1]}"], stderr=subprocess.PIPE, text=True)
    for _ in range(50):
        if sock.exists():
            break
        time.sleep(0.05)
    yield sock, got
    p.kill()
    p.wait()
    up.shutdown()


def _http(sock, raw):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(5)
    s.connect(str(sock))
    s.sendall(raw)
    out = b""
    try:
        while chunk := s.recv(65536):
            out += chunk
    except OSError:
        pass
    s.close()
    return out


def _post(path, body=b'{"model":"m","messages":[]}', extra=b""):
    return (b"POST " + path + b" HTTP/1.1\r\nHost: 127.0.0.1:11434\r\nContent-Type: application/json\r\n"
            + extra + b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)


def test_model_relay_passes_chat(model_relay):
    sock, got = model_relay
    body = b'{"model":"m","messages":[{"role":"user","content":"hi"}],"stream":true}'
    reply = _http(sock, _post(b"/v1/chat/completions", body, b"Connection: keep-alive\r\n"))
    assert reply.startswith(b"HTTP/1.1 200") and reply.endswith(b"ok")
    (req,) = got
    head, _, sent = req.partition(b"\r\n\r\n")
    assert sent == body and b"connection: close" in head.lower() and b"keep-alive" not in head.lower()


@pytest.mark.parametrize("path", [b"/api/pull", b"/api/push", b"/api/delete", b"/api/create", b"/api/copy",
                                  b"/api/generate", b"/v1/chat/completions/../../api/pull"])
def test_model_relay_refuses_other_endpoints(model_relay, path):
    sock, got = model_relay
    reply = _http(sock, _post(path, b'{"model":"evil.example/x/y","insecure":true}'))
    assert reply.startswith(b"HTTP/1.1 403") and got == []


def test_model_relay_allows_read_only_gets(model_relay):
    sock, got = model_relay
    for path in (b"/api/version", b"/api/tags", b"/v1/models"):
        assert _http(sock, b"GET " + path + b" HTTP/1.1\r\nHost: x\r\n\r\n").startswith(b"HTTP/1.1 200")
    assert len(got) == 3


def test_model_relay_one_request_per_connection(model_relay):
    sock, got = model_relay
    smuggled = _post(b"/v1/chat/completions") + _post(b"/api/pull", b'{"model":"evil.example/x"}')
    _http(sock, smuggled)
    time.sleep(0.3)
    assert len(got) == 1 and b"/api/pull" not in got[0]


@pytest.mark.parametrize("raw", [
    b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n",
    b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\ntransfer-encoding: chunked\r\nContent-Length: 5\r\n\r\nhello",
    b"POST http://evil.example/api/pull HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}",
    b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n\r\n",  # a POST with no length
    b"GARBAGE\r\n\r\n",
])
def test_model_relay_refuses_ambiguous_requests(model_relay, raw):
    sock, got = model_relay
    assert _http(sock, raw).startswith(b"HTTP/1.1 4") and got == []


# --- the clipboard relay (security audit 2026-11-13, finding 7): Qwen can set Wai's Windows
# clipboard, so a later paste into a terminal could run what Qwen chose. Escape sequences
# (an early end-of-bracketed-paste lets the rest run), other control characters, carriage
# returns, and the bidi and zero-width characters that hide text are removed; tabs and
# newlines stay, so copied code keeps its shape.

def test_clip_text_drops_controls_and_hiding_characters():
    raw = ("ls\x1b[201~rm -rf ~\r\n\tok\x07\x00\x9b31m" "\u202eevil\u202c" "zero\u200bwidth\u2066x\u2069\n").encode()
    assert relay.clip_text(raw) == b"ls[201~rm -rf ~\n\tok31mevilzerowidthx\n"


def test_clip_text_keeps_plain_text_and_bad_bytes_safe():
    assert relay.clip_text(b"def f():\n\treturn 1\n") == b"def f():\n\treturn 1\n"
    assert relay.clip_text(b"caf\xc3\xa9 \xff") == "café �".encode()
