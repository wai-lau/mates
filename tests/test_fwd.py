import asyncio, json, threading, http.server, socket, time, tempfile, os, urllib.request, urllib.error
import fwd

SSE = (b'event: message_start\ndata: {"type":"message_start","message":{"model":"mates","usage":{"input_tokens":1200,"output_tokens":1}}}\n\n'
       b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}\n\n'
       b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":340}}\n\n')

def test_parse_usage_streaming():
    assert fwd.parse_usage(SSE) == {"response_model": "mates", "input_tokens": 1200, "output_tokens": 340}

def test_parse_usage_json():
    body = json.dumps({"model": "mates", "usage": {"input_tokens": 1200, "output_tokens": 340}}).encode()
    assert fwd.parse_usage(body) == {"response_model": "mates", "input_tokens": 1200, "output_tokens": 340}

def _fake_upstream(model):
    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            b = json.dumps({"model": model, "usage": {"input_tokens": 5, "output_tokens": 7}}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def log_message(self, *a): pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv

def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

def _start_pair(tmp, upstream_port):
    sock = os.path.join(tmp, "o.sock"); log = os.path.join(tmp, "f.jsonl"); port = _free_port()
    for coro in (fwd.serve_out(sock, ("127.0.0.1", upstream_port), log, "mates"),
                 fwd.serve_in(sock, ("127.0.0.1", port))):
        threading.Thread(target=asyncio.run, args=(coro,), daemon=True).start()
        time.sleep(0.3)
    return port, log

def _post(port, path, model):
    data = json.dumps({"model": model, "max_tokens": 5, "messages": []}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r: return r.status
    except urllib.error.HTTPError as e: return e.code

def _lines(log):
    return [json.loads(l) for l in open(log)]

def test_records_response_model():
    up = _fake_upstream("other")
    with tempfile.TemporaryDirectory() as tmp:
        port, log = _start_pair(tmp, up.server_address[1])
        assert _post(port, "/v1/messages", "mates") == 200
        time.sleep(0.2)
        rec = _lines(log)[-1]
        assert rec["requested_model"] == "mates" and rec["response_model"] == "other"
        assert rec["input_tokens"] == 5 and rec["output_tokens"] == 7 and rec["status"] == 200

def test_rejects_wrong_path_and_model():
    up = _fake_upstream("mates")
    with tempfile.TemporaryDirectory() as tmp:
        port, log = _start_pair(tmp, up.server_address[1])
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/tags")
        try: code = urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as e: code = e.code
        assert code == 403
        assert _post(port, "/v1/messages", "x") == 403
        time.sleep(0.2)
        assert [r["status"] for r in _lines(log)] == [403, 403]
