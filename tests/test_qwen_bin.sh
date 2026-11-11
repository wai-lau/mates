#!/usr/bin/env bash
# bin/qwen: exec mode, read-only git, per-repo OpenCode state, GPU lock, runtime mount,
# host/warm. No model and no real server: pgrep/ollama/curl/systemctl/claude/pbcopy are
# PATH stubs (hosaka is never stopped, no digest call), OpenCode is a stub that sleeps,
# and XDG_STATE_HOME/XDG_CONFIG_HOME are temp dirs.
#   bash tests/test_qwen_bin.sh
set -uo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
QWEN=$HERE/bin/qwen
command -v bwrap >/dev/null || { echo "SKIP: bwrap not installed"; exit 0; }

T=$(mktemp -d)
cleanup() { pkill -f "$T/stub/ollama" 2>/dev/null; rm -rf "$T"; }
trap cleanup EXIT

mkdir -p "$T/stub" "$T/state" "$T/config"
stub() { printf '#!/bin/sh\n%s\n' "$2" > "$T/stub/$1"; chmod +x "$T/stub/$1"; }
stub pgrep 'exit 1'
stub ollama "touch '$T/ollama-started'; sleep 3"  # no exec: pkill -f finds it by path
stub curl "echo \"\$*\" >> '$T/curl.log'; exit 0"
stub systemctl "echo \"\$*\" >> '$T/systemctl.log'; exit 0"
stub claude 'exit 1'
stub pbcopy "cat > '$T/pbcopy.out'"
printf '#!/bin/sh\nsleep 2\n' > "$T/opencode"; chmod +x "$T/opencode"
export PATH="$T/stub:$PATH" XDG_STATE_HOME=$T/state XDG_CONFIG_HOME=$T/config QWEN_OPENCODE=$T/opencode
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
unset QWEN_GIT_RO QWEN_RUNTIME

mkrepo() { git init -q "$1" && echo hi > "$1/a.txt" && git -C "$1" add a.txt && git -C "$1" commit -qm init; }
mkrepo "$T/repo"; mkdir "$T/repo/sub"
git -C "$T/repo" worktree add -q -b wt "$T/wt"
mkrepo "$T/other"

fail=0
ok() { echo "PASS $1"; }
bad() { echo "FAIL $1"; fail=1; }
# check <name> <condition string>: eval the condition. Capture $? into rc first; check cannot see it.
check() { local name=$1; shift; if eval "$*"; then ok "$name"; else bad "$name"; fi; }
in_() { local d=$1; shift; (cd "$d" && "$@"); }

# --- exec ---------------------------------------------------------------
in_ "$T/repo" "$QWEN" exec -- sh -c 'exit 7' 2>/dev/null; rc=$?
check "exec returns the command's exit code (7)" '[ "$rc" = 7 ]'
in_ "$T/repo" "$QWEN" exec -- true 2>/dev/null; rc=$?
check "exec returns 0 for true" '[ "$rc" = 0 ]'
in_ "$T/repo" "$QWEN" exec -- sh -c 'test ! -e "$1/.ssh" && test ! -e "$1/.claude" && test ! -e "$1/src"' sh "$HOME" 2>/dev/null; rc=$?
check "exec cannot reach $HOME (.ssh, .claude, src)" '[ "$rc" = 0 ]'
in_ "$T/repo" "$QWEN" exec -- sh -c 'test ! -e "$1"' sh "$HOME" 2>/dev/null; rc=$?
check "exec sees no $HOME at all" '[ "$rc" = 0 ]'
rm -f "$T/ollama-started" "$T/curl.log" "$T/systemctl.log"
out=$(in_ "$T/repo" "$QWEN" exec -- bash -c 'ls -A /run/qwen; (exec 3<>/dev/tcp/127.0.0.1/11434) 2>/dev/null && echo LISTENING; echo "relay=${QWEN_NO_RELAY:-}"' 2>/dev/null)
check "exec starts no ollama" '[ ! -e "$T/ollama-started" ]'
check "exec probes no server and touches no systemd unit" '[ ! -e "$T/curl.log" ] && [ ! -e "$T/systemctl.log" ]'
check "exec starts no relay (no sockets, nothing on 11434)" 'bash -c '\''! grep -qE "ollama.sock|clip.sock|LISTENING" <<<"$1" && grep -qx relay=1 <<<"$1"'\'' _ "$out"'
in_ "$T/repo" "$QWEN" exec -- sleep 1 2>/dev/null & p=$!; sleep 0.5
check "exec writes no GPU lock" '[ -z "$(ls -A "$T/state/qwen/locks" 2>/dev/null)" ]'
wait $p
check "exec leaves no run dir behind" '[ -z "$(ls -d "$T/state/qwen"/run.* 2>/dev/null)" ]'
in_ "$T/repo" "$QWEN" exec 2>/dev/null; rc=$?
check "exec with no command fails" '[ "$rc" != 0 ]'

# --- read-only git ------------------------------------------------------
in_ "$T/repo" "$QWEN" exec -- git update-ref refs/heads/rw-control HEAD 2>/dev/null
check "control: without QWEN_GIT_RO a branch ref can be written" 'git -C "$T/repo" rev-parse -q --verify refs/heads/rw-control >/dev/null'
git -C "$T/repo" branch -qD rw-control
for d in repo wt; do
  w=$T/$d
  QWEN_GIT_RO=1 in_ "$w" "$QWEN" exec -- git update-ref refs/heads/x HEAD 2>/dev/null; rc=$?
  check "[$d] RO: git update-ref fails" '[ "$rc" != 0 ]'
  check "[$d] RO: no ref x on the host" 'bash -c '\''! git -C "$1" rev-parse -q --verify refs/heads/x >/dev/null'\'' _ "$w"'
  QWEN_GIT_RO=1 in_ "$w" "$QWEN" exec -- sh -c 'echo new > new.txt && git add new.txt' 2>/dev/null; rc=$?
  check "[$d] RO: git add fails" '[ "$rc" != 0 ]'
  check "[$d] RO: nothing staged on the host" '[ -z "$(git -C "$w" diff --cached --name-only)" ]'
  rm -f "$w/new.txt"
  QWEN_GIT_RO=1 in_ "$w" "$QWEN" exec -- git hash-object -w a.txt >/dev/null 2>&1; rc=$?
  check "[$d] RO: git hash-object -w fails" '[ "$rc" != 0 ]'
  QWEN_GIT_RO=1 in_ "$w" "$QWEN" exec -- mv .git x 2>/dev/null; rc=$?
  check "[$d] RO: mv .git x fails" '[ "$rc" != 0 ] && [ -e "$w/.git" ] && [ ! -e "$w/x" ]'
  QWEN_GIT_RO=1 in_ "$w" "$QWEN" exec -- sh -c 'echo edit > a.txt && git status --short && git diff --stat' >/dev/null 2>&1; rc=$?
  check "[$d] RO: git status and git diff work" '[ "$rc" = 0 ]'
  git -C "$w" checkout -q -- a.txt
  QWEN_GIT_RO=1 in_ "$w" "$QWEN" exec -- sh -c 'echo edit > a.txt' 2>/dev/null
  check "[$d] RO: the work tree itself stays writable" 'grep -qx edit "$w/a.txt"'
  git -C "$w" checkout -q -- a.txt
done
QWEN_GIT_RO=1 in_ "$T/wt" "$QWEN" exec -- sh -c 'echo "gitdir: /tmp/elsewhere" > .git' 2>/dev/null; rc=$?
check "[wt] RO: the .git file cannot be repointed" '[ "$rc" != 0 ] && grep -q '\''^gitdir: .*/worktrees/'\'' "$T/wt/.git"'

# --- runtime mount ------------------------------------------------------
mkdir -p "$T/rt/bin"; printf '#!/bin/sh\necho hello-from-runtime\n' > "$T/rt/bin/rt-hello"; chmod +x "$T/rt/bin/rt-hello"
cat > "$T/rt/env" <<EOF
PATH=$T/rt/bin
FOO=bar
_U=a=b
SP=a b c
EMPTY=
LANG=en_CA.UTF-8
# a comment
export X=1
1BAD=x
 LEAD=1
bad-name=1
EOF
p=$(QWEN_RUNTIME=$T/rt in_ "$T/repo" "$QWEN" exec -- sh -c 'echo "$PATH"' 2>/dev/null)
check "runtime PATH comes first" '[ "${p%%:*}" = "$T/rt/bin" ]'
check "runtime PATH keeps the default after it" 'bash -c '\''[[ $1 == *:/opt/opencode:* && $1 == *:/usr/bin:/bin ]]'\'' _ "$p"'
h=$(QWEN_RUNTIME=$T/rt in_ "$T/repo" "$QWEN" exec -- rt-hello 2>/dev/null)
check "runtime bin runs inside" '[ "$h" = hello-from-runtime ]'
QWEN_RUNTIME=$T/rt in_ "$T/repo" "$QWEN" exec -- sh -c 'echo x > "$1/w"' sh "$T/rt" 2>/dev/null; rc=$?
check "runtime dir is read-only" '[ "$rc" != 0 ] && [ ! -e "$T/rt/w" ]'
base=$(in_ "$T/repo" "$QWEN" exec -- env 2>/dev/null | sort)
with=$(QWEN_RUNTIME=$T/rt in_ "$T/repo" "$QWEN" exec -- env 2>/dev/null | sort)
bash_kept=$(comm -13 <(echo "$base") <(echo "$with") | grep -v '^PATH=' | sort)
py_kept=$(python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); import runtime
print("\n".join(l for l in runtime.runtime_env(sys.argv[2]) if not l.startswith("PATH=")))' "$HERE/mates" "$T/rt" | sort)
check "bash and runtime.runtime_env keep the same env lines" '[ "$bash_kept" = "$py_kept" ]'
[ "$bash_kept" = "$py_kept" ] || { echo "  bash: $bash_kept"; echo "  py:   $py_kept"; }
QWEN_RUNTIME=$T/nope in_ "$T/repo" "$QWEN" exec -- true 2>/dev/null; rc=$?
check "a missing runtime dir refuses to run" '[ "$rc" != 0 ]'

# --- per-repo OpenCode state -------------------------------------------
key() { printf '%s' "$1" | sed 's/[^A-Za-z0-9]/-/g'; }
d_repo=$(in_ "$T/repo" "$QWEN" opencode-dir); d_sub=$(in_ "$T/repo/sub" "$QWEN" opencode-dir)
d_wt=$(in_ "$T/wt" "$QWEN" opencode-dir); d_other=$(in_ "$T/other" "$QWEN" opencode-dir)
check "opencode-dir is \$STATE/opencode/<key of main checkout>" '[ "$d_repo" = "$T/state/qwen/opencode/$(key "$T/repo")" ]'
check "opencode-dir is the same in a subdir and in a worktree" '[ "$d_sub" = "$d_repo" ] && [ "$d_wt" = "$d_repo" ]'
check "opencode-dir differs for another repo" '[ -n "$d_other" ] && [ "$d_other" != "$d_repo" ]'
in_ "$T/wt" "$QWEN" exec -- sh -c 'echo s > ~/.local/share/opencode/probe' 2>/dev/null
check "the sandbox's OpenCode folder is the repo's folder" '[ -f "$d_repo/probe" ]'
in_ "$T/other" "$QWEN" exec -- sh -c 'test ! -e ~/.local/share/opencode/probe' 2>/dev/null; rc=$?
check "another repo's sandbox does not see it" '[ "$rc" = 0 ]'
( cd "$T" && "$QWEN" opencode-dir >/dev/null 2>&1 ); rc=$?
check "opencode-dir outside a repo fails" '[ "$rc" != 0 ]'

# --- GPU lock -----------------------------------------------------------
rm -f "$T/ollama-started"
(cd "$T/repo" && exec "$QWEN" >/dev/null 2>&1) & q=$!  # exec: $q is the qwen pid
seen=""; for _ in $(seq 40); do [ -e "$T/state/qwen/locks/$q" ] && { seen=1; break; }; sleep 0.1; done
check "a lock named after the qwen pid exists while the TUI runs" '[ -n "$seen" ]'
wait $q; rc=$?
check "the stub TUI run exits 0" '[ "$rc" = 0 ]'
check "the lock is gone after" '[ ! -e "$T/state/qwen/locks/$q" ] && [ -z "$(ls -A "$T/state/qwen/locks")" ]'
check "the run dir is gone after" '[ -z "$(ls -d "$T/state/qwen"/run.* 2>/dev/null)" ]'
check "the TUI run started the (stub) server" '[ -e "$T/ollama-started" ]'
(cd "$T/repo" && exec "$QWEN" >/dev/null 2>&1) & q=$!  # exec: $q is the qwen pid
for _ in $(seq 40); do [ -e "$T/state/qwen/locks/$q" ] && break; sleep 0.1; done
kill -TERM $q; wait $q
check "the lock is gone after SIGTERM" '[ ! -e "$T/state/qwen/locks/$q" ]'

# --- host / warm --------------------------------------------------------
pkill -f "$T/stub/ollama"; sleep 0.2  # stub servers from the runs above
rm -f "$T/ollama-started" "$T/curl.log"
h=$("$QWEN" host 2>/dev/null); rc=$?
check "host exits 1 with no server, prints nothing" '[ "$rc" = 1 ] && [ -z "$h" ]'
check "host starts no server" '[ ! -e "$T/ollama-started" ]'
"$QWEN" warm >/dev/null 2>&1; rc=$?
check "warm exits 0" '[ "$rc" = 0 ]'
for _ in $(seq 20); do [ -e "$T/ollama-started" ] && break; sleep 0.1; done
check "warm starts the (stub) server" '[ -e "$T/ollama-started" ]'
for _ in $(seq 20); do grep -q api/generate "$T/curl.log" 2>/dev/null && break; sleep 0.1; done
check "warm asks the server to load the model" 'grep -q api/generate "$T/curl.log"'

# --- sandbox-test lines -------------------------------------------------
st=$(QWEN_GIT_RO=1 in_ "$T/wt" "$QWEN" sandbox-test 2>/dev/null)
for l in "git ref write: blocked" "git object write: blocked" "worktree .git file edit: blocked" \
         "other repos' OpenCode state: blocked" "git status: REACHABLE" "your home (~/.ssh): blocked"; do
  check "sandbox-test (RO, worktree): $l" 'grep -qF "$l" <<<"$st"'
done
check "sandbox-test: the clipboard relay still works (write reached pbcopy)" 'grep -qF "clipboard write: REACHABLE" <<<"$st" && grep -qx "qwen sandbox-test" "$T/pbcopy.out"'
check "sandbox-test: clipboard read stays blocked" 'grep -qF "clipboard read: blocked" <<<"$st"'
check "sandbox-test left no probe files in git" '[ -z "$(find "$T/repo/.git" -name '\''qwen-probe*'\'')" ]'

[ $fail = 0 ] && echo "ALL PASS" || echo "SOME FAILED"
exit $fail
