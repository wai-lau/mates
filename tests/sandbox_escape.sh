#!/usr/bin/env bash
# Spec First step 4: everything a worker must not reach, tried from inside the
# sandbox (must all fail) and with only the user switch (file/git must fail).
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
T=/srv/mates/tasks/escape-test
RT=/srv/mates/runtimes/escape-test/0
umask 002
cleanup() { [ -n "${UP:-}" ] && kill "$UP" 2>/dev/null; [ -n "${FWD:-}" ] && kill "$FWD" 2>/dev/null; git -C "$HERE" worktree remove --force "$T/wt" 2>/dev/null || true; rm -rf "$T" "$RT"; }
trap cleanup EXIT
cleanup; FWD= UP=
mkdir -p "$T"/{ctl,state,sock} ~/.claude/mates/fwd; chmod 2770 "$T/state" "$T/sock"
git -C "$HERE" worktree add -q --detach "$T/wt" HEAD
mkdir -p "$T/wt/.mates"
install -m 644 "$HERE/tests/sandbox_probe.py" "$T/ctl/check.sh"
echo '{}' > "$T/ctl/settings.json"; echo "probe" > "$T/ctl/CLAUDE.md"; chmod 644 "$T/ctl/"*
echo canary > ~/.mates-canary; chmod 600 ~/.mates-canary
# Dummy upstream: the test checks the route through the forwarder, not a model (a real Ollama would load one).
python3 -c 'import http.server as h
class H(h.BaseHTTPRequestHandler):
    def do_POST(s):
        s.rfile.read(int(s.headers["Content-Length"])); b=b"{\"model\":\"probe-model\"}"
        s.send_response(200); s.send_header("Content-Length",str(len(b))); s.end_headers(); s.wfile.write(b)
    def log_message(s,*a): pass
h.HTTPServer(("127.0.0.1",11499),H).serve_forever()' & UP=$!
python3 "$HERE/mates/fwd.py" out --sock "$T/sock/ollama.sock" --upstream 127.0.0.1:11499 \
  --log ~/.claude/mates/fwd/escape-test.jsonl --model probe-model & FWD=$!
sleep 0.5
mkdir -p "$RT"; echo marker > "$RT/marker"; echo "MATES_PROBE_RT=$RT" > "$RT/env"

fail=0
judge() { # mode key expected-pattern
  local got; got=$(grep "^$2=" "$OUT" | cut -d= -f2- || true)
  if [[ "$got" =~ $3 ]]; then echo "PASS [$1] $2=$got"; else echo "FAIL [$1] $2=$got (want $3)"; fail=1; fi
}
OUT=$(mktemp)
"$HERE/mates/sandbox.sh" --task-dir "$T" --runtime "$RT" -- python3 .mates/check.sh > "$OUT" 2>&1 || true
for k in canary wai_cred claude_cred ls_src emet_data tcp_out dns git_write check_write; do judge bwrap "$k" '^denied'; done
judge bwrap runtime_read '^allowed$'; judge bwrap runtime_write '^denied'; judge bwrap uid '^1001$'; judge bwrap env_secrets '^none$'; judge bwrap ollama_fwd '^reached$'
"$HERE/mates/sandbox.sh" --task-dir "$T" --no-bwrap-for-test -- python3 "$T/ctl/check.sh" > "$OUT" 2>&1 || true
for k in canary wai_cred ls_src emet_data git_write; do judge user-only "$k" '^denied'; done
judge user-only uid '^1001$'; judge user-only env_secrets '^none$'
for k in claude_cred tcp_out dns; do echo "INFO [user-only] $(grep "^$k=" "$OUT" || true)"; done
rm -f "$OUT"
exit $fail
