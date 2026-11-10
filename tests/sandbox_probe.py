#!/usr/bin/env python3
"""Runs INSIDE the worker sandbox. Prints key=value per probe; the escape test judges them."""
import getpass, json, os, socket, urllib.request

def can_read(p):
    try:
        with open(p, "rb") as f: f.read(1)
        return "allowed"
    except OSError as e: return f"denied({e.errno})"

def can_list(p):
    try: os.listdir(p); return "allowed"
    except OSError as e: return f"denied({e.errno})"

def can_write(p):
    try:
        with open(p, "ab") as f: f.write(b"")
        return "allowed"
    except OSError as e: return f"denied({e.errno})"

out = {}
out["user"] = getpass.getuser() if os.environ.get("USER") else str(os.getuid())
out["uid"] = str(os.getuid())
bad = [k for k in os.environ if any(t in k.upper() for t in ("TOKEN", "KEY", "SECRET", "PASSWORD")) and k != "ANTHROPIC_AUTH_TOKEN"]
out["env_secrets"] = ",".join(bad) or "none"
out["canary"] = can_read("/home/wai/.mates-canary")
out["wai_cred"] = can_read("/home/wai/.claude/.credentials.json")
out["claude_cred"] = can_read("/home/claude/.claude/.credentials.json")
out["ls_src"] = can_list("/home/wai/src")
out["emet_data"] = can_list("/home/wai/src/emet/data")
s = socket.socket(); s.settimeout(3)
try: s.connect(("1.1.1.1", 443)); out["tcp_out"] = "allowed"
except OSError as e: out["tcp_out"] = f"denied({e.errno})"
finally: s.close()
try: socket.getaddrinfo("example.com", 443); out["dns"] = "allowed"
except OSError as e: out["dns"] = f"denied({e.errno})"
gitdir = None
try:
    gitdir = open(".git").read().split("gitdir:", 1)[1].strip()
except (OSError, IndexError): pass
out["git_write"] = can_write(os.path.join(gitdir, "HEAD")) if gitdir else "denied(nogit)"
out["check_write"] = can_write(".mates/check.sh") if os.path.exists(".mates/check.sh") else "absent"
try:
    req = urllib.request.Request("http://127.0.0.1:11434/v1/messages", method="POST",
        data=json.dumps({"model": "qwen3.8-mates", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}).encode(),
        headers={"Content-Type": "application/json", "anthropic-version": "2023-06-01"})
    try: urllib.request.urlopen(req, timeout=20); out["ollama_fwd"] = "reached"
    except urllib.error.HTTPError: out["ollama_fwd"] = "reached"
except OSError as e: out["ollama_fwd"] = f"unreachable({e})"
rt = os.environ.get("MATES_PROBE_RT")
if rt:
    out["runtime_read"] = can_read(os.path.join(rt, "marker"))
    out["runtime_write"] = can_write(os.path.join(rt, "marker"))
for k, v in out.items(): print(f"{k}={v}")
