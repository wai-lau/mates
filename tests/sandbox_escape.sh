#!/usr/bin/env bash
# Spec First step 4: everything a worker must not reach, tried from inside the
# sandbox (must all fail) and with only the user switch (file/git must fail).
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
T=/srv/mates/tasks/escape-test
umask 002
cleanup() { [ -n "${FWD:-}" ] && kill "$FWD" 2>/dev/null; git -C "$HERE" worktree remove --force "$T/wt" 2>/dev/null || true; rm -rf "$T"; }
trap cleanup EXIT
cleanup; FWD=
mkdir -p "$T"/{ctl,state,sock} ~/.claude/mates/fwd
git -C "$HERE" worktree add -q --detach "$T/wt" HEAD
mkdir -p "$T/wt/.mates"
install -m 644 "$HERE/tests/sandbox_probe.py" "$T/ctl/check.sh"
echo '{}' > "$T/ctl/settings.json"; echo "probe" > "$T/ctl/CLAUDE.md"; chmod 644 "$T/ctl/"*
echo canary > ~/.mates-canary; chmod 600 ~/.mates-canary
python3 "$HERE/mates/fwd.py" out --sock "$T/sock/ollama.sock" --upstream 127.0.0.1:11434 \
  --log ~/.claude/mates/fwd/escape-test.jsonl --model qwen3.8-mates & FWD=$!
sleep 0.5

fail=0
judge() { # mode key expected-pattern
  local got; got=$(grep "^$2=" "$OUT" | cut -d= -f2- || true)
  if [[ "$got" =~ $3 ]]; then echo "PASS [$1] $2=$got"; else echo "FAIL [$1] $2=$got (want $3)"; fail=1; fi
}
OUT=$(mktemp)
"$HERE/mates/sandbox.sh" --task-dir "$T" -- python3 .mates/check.sh > "$OUT" 2>&1 || true
for k in canary wai_cred claude_cred ls_src emet_data tcp_out dns git_write check_write; do judge bwrap "$k" '^denied'; done
judge bwrap uid '^1001$'; judge bwrap env_secrets '^none$'; judge bwrap ollama_fwd '^reached$'
"$HERE/mates/sandbox.sh" --task-dir "$T" --no-bwrap-for-test -- python3 "$T/ctl/check.sh" > "$OUT" 2>&1 || true
for k in canary wai_cred ls_src emet_data git_write; do judge user-only "$k" '^denied'; done
judge user-only uid '^1001$'; judge user-only env_secrets '^none$'
for k in claude_cred tcp_out dns; do echo "INFO [user-only] $(grep "^$k=" "$OUT" || true)"; done
rm -f "$OUT"
exit $fail
