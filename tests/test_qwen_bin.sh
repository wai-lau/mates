#!/usr/bin/env bash
# bin/qwen: exec mode, read-only git, per-repo OpenCode state, GPU lock, runtime mount,
# host/warm. No model and no real server: pgrep/ollama/curl/systemctl/claude/pbcopy are
# PATH stubs (hosaka is never stopped, no digest call), OpenCode is a stub that sleeps,
# and XDG_STATE_HOME/XDG_CONFIG_HOME are temp dirs.
#   bash tests/test_qwen_bin.sh
# shellcheck disable=SC2034  # variables read inside check '...' strings, which eval runs
set -uo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
QWEN=$HERE/bin/qwen
command -v bwrap >/dev/null || { echo "SKIP: bwrap not installed"; exit 0; }

T=$(mktemp -d)
RT=""
cleanup() { pkill -f "$T/stub/ollama" 2>/dev/null; rm -rf "$T"; [ -z "$RT" ] || rm -rf "$RT"; [ -z "${RT2:-}" ] || rm -rf "$RT2"; [ -z "${PF:-}" ] || rm -rf "$PF"; }
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
export QWEN_SRC=$T  # run by hand, qwen mounts only a project under ~/src: here, under $T

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

( cd "$T/repo" && env -u QWEN_OPENCODE HOME="$T/no-opencode-home" "$QWEN" exec -- true ) 2>/dev/null; rc=$?
check "exec works with no OpenCode installed" '[ "$rc" = 0 ]'

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
# A runtime must sit where no other account can write, up to / (I4): a private folder
# under the user runtime folder, not $T (under /tmp).
RT=$(mktemp -d "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/qwen-test-rt.XXXXXX")
mkdir -p "$RT/bin"; printf '#!/bin/sh\necho hello-from-runtime\n' > "$RT/bin/rt-hello"; chmod +x "$RT/bin/rt-hello"
cat > "$RT/env" <<EOF
PATH=$RT/bin
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
p=$(QWEN_RUNTIME=$RT in_ "$T/repo" "$QWEN" exec -- sh -c 'echo "$PATH"' 2>/dev/null)
check "runtime PATH comes first" '[ "${p%%:*}" = "$RT/bin" ]'
check "runtime PATH keeps the default after it" 'bash -c '\''[[ $1 == *:/opt/opencode:* && $1 == *:/usr/bin:/bin ]]'\'' _ "$p"'
h=$(QWEN_RUNTIME=$RT in_ "$T/repo" "$QWEN" exec -- rt-hello 2>/dev/null)
check "runtime bin runs inside" '[ "$h" = hello-from-runtime ]'
QWEN_RUNTIME=$RT in_ "$T/repo" "$QWEN" exec -- sh -c 'echo x > "$1/w"' sh "$RT" 2>/dev/null; rc=$?
check "runtime dir is read-only" '[ "$rc" != 0 ] && [ ! -e "$RT/w" ]'
base=$(in_ "$T/repo" "$QWEN" exec -- env 2>/dev/null | sort)
with=$(QWEN_RUNTIME=$RT in_ "$T/repo" "$QWEN" exec -- env 2>/dev/null | sort)
bash_kept=$(comm -13 <(echo "$base") <(echo "$with") | grep -v '^PATH=' | sort)
py_kept=$(python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); import runtime
print("\n".join(l for l in runtime.runtime_env(sys.argv[2]) if not l.startswith("PATH=")))' "$HERE/mates" "$RT" | sort)
check "bash and runtime.runtime_env keep the same env lines" '[ "$bash_kept" = "$py_kept" ]'
[ "$bash_kept" = "$py_kept" ] || { echo "  bash: $bash_kept"; echo "  py:   $py_kept"; }
mkdir -p "$T/shared-rt"; cp "$RT/env" "$T/shared-rt/env"
QWEN_RUNTIME=$T/shared-rt in_ "$T/repo" "$QWEN" exec -- true 2>/dev/null; rc=$?
check "a runtime under a folder others can write (/tmp): refused (I4)" '[ "$rc" != 0 ]'
QWEN_RUNTIME=$T/nope in_ "$T/repo" "$QWEN" exec -- true 2>/dev/null; rc=$?
check "a missing runtime dir refuses to run" '[ "$rc" != 0 ]'
# The env file is read on the host: a link (to ~/.secrets, say) or a fifo must be refused, never read.
RT2=$(mktemp -d "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/qwen-test-rt2.XXXXXX")
echo "LEAK=from-a-secret" > "$T/secret-env"; ln -s "$T/secret-env" "$RT2/env"
out=$(QWEN_RUNTIME=$RT2 in_ "$T/repo" "$QWEN" exec -- env 2>&1); rc=$?
check "a runtime env that is a link: refused, never read" '[ "$rc" != 0 ] && ! grep -q LEAK <<<"$out"'
rm "$RT2/env"; mkfifo "$RT2/env"
QWEN_RUNTIME=$RT2 timeout 10 bash -c 'cd "$1" && "$2" exec -- true' _ "$T/repo" "$QWEN" 2>/dev/null; rc=$?
check "a runtime env that is a fifo: refused at once" '[ "$rc" != 0 ] && [ "$rc" != 124 ]'

# --- the lead's proof files: read-only at $MATES_PROOFS ------------------
PF=$(mktemp -d "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/qwen-test-pf.XXXXXX")
echo proof-ok > "$PF/check.sh"
out=$(QWEN_PROOFS=$PF in_ "$T/repo" "$QWEN" exec -- sh -c 'cat "$MATES_PROOFS/check.sh"' 2>/dev/null)
check "proof files are readable at \$MATES_PROOFS" '[ "$out" = proof-ok ]'
QWEN_PROOFS=$PF in_ "$T/repo" "$QWEN" exec -- sh -c 'echo x > "$MATES_PROOFS/check.sh"' 2>/dev/null; rc=$?
check "proof files are read-only inside" '[ "$rc" != 0 ] && [ "$(cat "$PF/check.sh")" = proof-ok ]'
out=$(in_ "$T/repo" "$QWEN" exec -- sh -c 'echo "${MATES_PROOFS:-unset}"' 2>/dev/null)
check "no QWEN_PROOFS: MATES_PROOFS is unset inside" '[ "$out" = unset ]'
mkdir -p "$T/repo/proofs-in-repo"
QWEN_PROOFS=$T/repo/proofs-in-repo in_ "$T/repo" "$QWEN" exec -- true 2>/dev/null; rc=$?
check "a proofs folder inside the repo: refused (Qwen could edit it)" '[ "$rc" != 0 ]'
rmdir "$T/repo/proofs-in-repo"
mkdir -p "$T/shared-pf"
QWEN_PROOFS=$T/shared-pf in_ "$T/repo" "$QWEN" exec -- true 2>/dev/null; rc=$?
check "a proofs folder under a folder others can write (/tmp): refused" '[ "$rc" != 0 ]'
QWEN_PROOFS=$T/nope in_ "$T/repo" "$QWEN" exec -- true 2>/dev/null; rc=$?
check "a missing proofs folder refuses to run" '[ "$rc" != 0 ]'

# --- per-repo OpenCode state -------------------------------------------
key() { printf '%s' "$1" | sed 's/[^A-Za-z0-9]/-/g'; }
d_repo=$(in_ "$T/repo" "$QWEN" opencode-dir); d_sub=$(in_ "$T/repo/sub" "$QWEN" opencode-dir)
d_wt=$(in_ "$T/wt" "$QWEN" opencode-dir); d_other=$(in_ "$T/other" "$QWEN" opencode-dir)
check "opencode-dir is \$STATE/opencode/<key of main checkout>" '[ "$d_repo" = "$T/state/qwen/opencode/$(key "$T/repo")" ]'
check "opencode-dir is the same in a subdir and in a worktree" '[ "$d_sub" = "$d_repo" ] && [ "$d_wt" = "$d_repo" ]'
check "opencode-dir differs for another repo" '[ -n "$d_other" ] && [ "$d_other" != "$d_repo" ]'
in_ "$T/wt" "$QWEN" shell -c 'echo s > ~/.local/share/opencode/probe' >/dev/null 2>&1
check "the sandbox's OpenCode folder is the repo's folder" '[ -f "$d_repo/probe" ]'
in_ "$T/other" "$QWEN" shell -c 'test ! -e ~/.local/share/opencode/probe' >/dev/null 2>&1; rc=$?
check "another repo's sandbox does not see it" '[ "$rc" = 0 ]'
( cd "$T" && "$QWEN" opencode-dir >/dev/null 2>&1 ); rc=$?
check "opencode-dir outside a repo fails" '[ "$rc" != 0 ]'

in_ "$T/repo" "$QWEN" exec -- sh -c 'ls -A ~/.local/share/opencode; echo x > ~/.local/share/opencode/from-check' 2>/dev/null >"$T/oc.out"; rc=$?
check "exec gets an empty OpenCode folder, and its writes do not persist" '[ "$rc" = 0 ] && [ ! -s "$T/oc.out" ] && [ ! -e "$d_repo/from-check" ]'

# --- repo discovery cannot be steered by planted git files -------------
L=$T/lead; mkrepo "$L"; echo lead > "$L/lead.txt"
W=$L/.claude/worktrees/mates-qwen-1
git -C "$L" worktree add -q -b mates/qwen-1 "$W"
mkdir -p "$W/.mates/check/tree1"
fakegit() { git init -q "$T/fk" && mv "$T/fk/.git" "$1" && rmdir "$T/fk"; }  # a non-bare git dir, moved
fakegit "$W/.mates/fake"
git config --file "$W/.mates/fake/config" core.worktree "$L"
echo "gitdir: $W/.mates/fake" > "$W/.mates/.git"
C=$W/.mates/check/tree1
( cd "$C" && git rev-parse --show-toplevel ) > "$T/disc.out" 2>/dev/null
check "setup: host git discovery from the check folder is steered to the lead checkout" '[ "$(cat "$T/disc.out")" = "$L" ]'
plant='echo PLANTED > "$1/lead.txt"'
QWEN_GIT_RO=1 in_ "$C" "$QWEN" exec -- sh -c "$plant" sh "$L" 2>/dev/null; rc=$?
check "planted .mates/.git, no QWEN_ROOT: exec refuses" '[ "$rc" != 0 ] && grep -qx lead "$L/lead.txt"'
QWEN_GIT_RO=1 QWEN_ROOT=$W in_ "$C" "$QWEN" exec -- sh -c "$plant" sh "$L" 2>/dev/null; rc=$?
check "planted .mates/.git, QWEN_ROOT=worktree: the lead checkout is not mounted (host file unchanged)" 'grep -qx lead "$L/lead.txt"'
QWEN_GIT_RO=1 QWEN_ROOT=$W in_ "$C" "$QWEN" exec -- sh -c 'echo ok > "$1/check.txt" && git -C "$2" status --short >/dev/null' sh "$C" "$W" 2>/dev/null; rc=$?
check "QWEN_ROOT=worktree: exec from the check folder runs, git status works" '[ "$rc" = 0 ] && [ -f "$C/check.txt" ]'
QWEN_ROOT=$W in_ "$T/repo" "$QWEN" exec -- true 2>/dev/null; rc=$?
check "QWEN_ROOT with a cwd outside it: refused" '[ "$rc" != 0 ]'
mkdir -p "$W/sub"; fakegit "$W/sub-fake"; git config --file "$W/sub-fake/config" core.worktree "$L"
echo "gitdir: $W/sub-fake" > "$W/sub/.git"
in_ "$W/sub" "$QWEN" exec -- sh -c "$plant" sh "$L" 2>/dev/null; rc=$?
check "planted sub/.git with core.worktree elsewhere: refused" '[ "$rc" != 0 ] && grep -qx lead "$L/lead.txt"'
echo "gitdir: $T/other/.git" > "$W/sub/.git"
in_ "$W/sub" "$QWEN" exec -- true 2>/dev/null; rc=$?
check "planted sub/.git pointing at another repo's git dir: refused" '[ "$rc" != 0 ]'
git -C "$L" worktree add -q -b mates/qwen-2 "$L/.claude/worktrees/mates-qwen-2"
echo "gitdir: $L/.git/worktrees/mates-qwen-2" > "$W/sub/.git"
in_ "$W/sub" "$QWEN" exec -- true 2>/dev/null; rc=$?
check "planted sub/.git pointing at another worktree's admin dir: refused" '[ "$rc" != 0 ]'
rm -rf "$W/sub" "$W/sub-fake"
ln -s "$L/.git" "$T/symrepo-git"; mkdir "$T/symrepo"; ln -s "$L/.git" "$T/symrepo/.git"
in_ "$T/symrepo" "$QWEN" exec -- true 2>/dev/null; rc=$?
check "a .git symlink: refused" '[ "$rc" != 0 ]'
mkrepo "$T/cw"; git -C "$T/cw" config core.worktree "$L"
in_ "$T/cw" "$QWEN" exec -- true 2>/dev/null; rc=$?
check "main checkout whose core.worktree points elsewhere: refused" '[ "$rc" != 0 ]'
mkrepo "$T/inc"; git -C "$T/inc" config include.path "$T/inc-extra"
in_ "$T/inc" "$QWEN" exec -- true 2>/dev/null; rc=$?
check "git config with include: refused" '[ "$rc" != 0 ]'
git -C "$T/repo" -c protocol.file.allow=always submodule add -q "$T/other" sm >/dev/null 2>&1
in_ "$T/repo/sm" "$QWEN" exec -- git status --short 2>/dev/null >/dev/null; rc=$?
check "a real submodule still works" '[ "$rc" = 0 ]'
check "a submodule has its own OpenCode folder" '[ "$(in_ "$T/repo/sm" "$QWEN" opencode-dir)" != "$d_repo" ]'
# S1: a linked worktree of a submodule (mates in a lead that is itself a submodule)
SUP=$T/super; mkrepo "$SUP"
git -C "$SUP" -c protocol.file.allow=always submodule add -q "$T/other" apps/sm >/dev/null 2>&1
SMW=$SUP/apps/sm/.claude/worktrees/mates-qwen-1
git -C "$SUP/apps/sm" worktree add -q -b mates/qwen-1 "$SMW"
SMC=$(git -C "$SUP/apps/sm" rev-parse --path-format=absolute --git-common-dir)
QWEN_GIT_RO=1 QWEN_ROOT=$SMW in_ "$SMW" "$QWEN" exec -- git status --short >/dev/null 2>&1; rc=$?
check "S1: a submodule's linked worktree runs (shared core.worktree is the submodule's own)" '[ "$rc" = 0 ]'
old=$(git config --file "$SMC/config" core.worktree)
git config --file "$SMC/config" core.worktree "$T/other"
QWEN_GIT_RO=1 QWEN_ROOT=$SMW in_ "$SMW" "$QWEN" exec -- true 2>/dev/null; rc=$?
check "S1: a shared core.worktree pointing at another folder still refuses" '[ "$rc" != 0 ]'
mkdir -p "$T/decoy"; echo "gitdir: $T/other/.git" > "$T/decoy/.git"
git config --file "$SMC/config" core.worktree "$T/decoy"
QWEN_GIT_RO=1 QWEN_ROOT=$SMW in_ "$SMW" "$QWEN" exec -- true 2>/dev/null; rc=$?
check "S1: a shared core.worktree at a checkout of another git dir refuses" '[ "$rc" != 0 ]'
git config --file "$SMC/config" core.worktree "$old"
FH=$T/fakehome; mkrepo "$FH"
( cd "$FH" && HOME=$FH "$QWEN" exec -- true ) 2>/dev/null; rc=$?
check "a repo top of \$HOME: refused" '[ "$rc" != 0 ]'
mkdir -p "$FH/inner"
( cd "$FH" && HOME=$FH/inner "$QWEN" exec -- true ) 2>/dev/null; rc=$?
check "a repo top above \$HOME: refused" '[ "$rc" != 0 ]'
( cd / && QWEN_ROOT=/ "$QWEN" exec -- true ) 2>/dev/null; rc=$?
check "QWEN_ROOT=/: refused" '[ "$rc" != 0 ]'
( cd "$HOME" && QWEN_ROOT=$HOME "$QWEN" exec -- true ) 2>/dev/null; rc=$?
check "QWEN_ROOT=\$HOME: refused" '[ "$rc" != 0 ]'
mkdir -p "$T/rtwrap"; ( cd "$T/repo" && QWEN_RUNTIME=$T "$QWEN" exec -- true ) 2>/dev/null; rc=$?
check "a runtime dir that holds the repo: refused" '[ "$rc" != 0 ]'

# --- check mode: only the snapshot folder, at /work/check (I1) ----------
CK=$(mktemp -d "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/qwen-test-check.XXXXXX")
echo snap > "$CK/s.txt"
out=$(cd / && QWEN_CHECK_DIR=$CK "$QWEN" exec -- sh -c 'pwd; cat s.txt; echo w > made; test ! -e "$1" && test ! -e "$2" && echo nothing-else' sh "$T/repo" "$CK" 2>&1); rc=$?
check "QWEN_CHECK_DIR: runs in /work/check on the snapshot, repo and host path not mounted" '[ "$rc" = 0 ] && [ "$out" = "$(printf "/work/check\nsnap\nnothing-else")" ] && [ -f "$CK/made" ]'
(cd / && QWEN_CHECK_DIR=$T/repo "$QWEN" exec -- true) 2>/dev/null; rc=$?
check "QWEN_CHECK_DIR under a folder others can write (/tmp): refused" '[ "$rc" != 0 ]'
rm -rf "$CK"

# --- host-side Python never imports from the repo (C1) -------------------
P=$T/plant; mkrepo "$P"
for m in socket subprocess json; do printf 'open(%s, "a").write("%s\\n")\n' "'$T/planted-ran'" "$m" > "$P/$m.py"; done
in_ "$P" "$QWEN" server >/dev/null 2>&1
check "qwen server: a socket.py/subprocess.py/json.py in the cwd does not run on the host" '[ ! -e "$T/planted-ran" ]'
pkill -f "$T/stub/ollama"; sleep 0.2
(cd "$P" && exec "$QWEN" >/dev/null 2>&1); rc=$?
check "qwen (TUI start): planted modules in the repo do not run on the host" '[ ! -e "$T/planted-ran" ]'
pkill -f "$T/stub/ollama"; sleep 0.2
rm -rf "$P" "$T/planted-ran"

# --- digests never follow a CLAUDE.md link (C2) --------------------------
absent() { ! grep -rq "$1" "$2" 2>/dev/null; }
echo "SECRET-LINE-42" > "$T/secret"
stub claude "cat >> '$T/claude-in'; echo a-digest"
DL=$T/dlead; mkrepo "$DL"; echo "lead rules: be careful" > "$DL/CLAUDE.md"
git -C "$DL" add CLAUDE.md && git -C "$DL" commit -qm claude
DW=$DL/.claude/worktrees/mates-qwen-1; git -C "$DL" worktree add -q -b mates/qwen-1 "$DW"
ln -sf "$T/secret" "$DW/CLAUDE.md"
rm -rf "$T/claude-in" "$T/config/qwen/digests"
(cd "$DW" && QWEN_ROOT=$DW QWEN_GIT_RO=1 exec "$QWEN" >/dev/null 2>&1)
check "worktree CLAUDE.md -> secret: the secret never reaches claude -p" 'absent SECRET-LINE-42 "$T/claude-in"'
check "worktree CLAUDE.md -> secret: no cached digest holds it" 'absent SECRET-LINE-42 "$T/config/qwen/digests"'
check "worktree: the repo digest comes from the lead's committed CLAUDE.md" 'grep -q "lead rules: be careful" "$T/claude-in"'
PR=$T/plainrepo; mkrepo "$PR"; ln -s "$T/secret" "$PR/CLAUDE.md"
rm -rf "$T/claude-in" "$T/config/qwen/digests"
(cd "$PR" && exec "$QWEN" >/dev/null 2>&1)
check "plain repo CLAUDE.md -> secret: refused, the secret never reaches claude -p" 'absent SECRET-LINE-42 "$T/claude-in"'
rm -f "$PR/CLAUDE.md"; echo "plain rules" > "$PR/CLAUDE.md"; rm -rf "$T/claude-in" "$T/config/qwen/digests"
(cd "$PR" && exec "$QWEN" >/dev/null 2>&1)
check "plain repo with a regular CLAUDE.md: it is digested" 'grep -q "plain rules" "$T/claude-in"'
stub claude "cat >/dev/null; seq 1 400 | tr '\\n' ' '; echo"
rm -rf "$T/config/qwen/digests"
(cd "$PR" && exec "$QWEN" >/dev/null 2>&1)
check "a digest over 150 words is cut to 150" 'for f in "$T"/config/qwen/digests/*.md; do w=$(wc -w < "$f"); [ "$w" -le 150 ] && [ "$w" -gt 100 ] || exit 1; done; ls "$T"/config/qwen/digests/*.md >/dev/null'
stub claude 'exit 1'
pkill -f "$T/stub/ollama"; sleep 0.2
rm -rf "$DL" "$PR" "$T/claude-in"

# --- GPU lock -----------------------------------------------------------
rm -f "$T/ollama-started"
(cd "$T/repo" && exec "$QWEN" >/dev/null 2>&1) & q=$!  # exec: $q is the qwen pid
seen=""; for _ in $(seq 40); do [ -e "$T/state/qwen/locks/$q" ] && { seen=1; break; }; sleep 0.1; done
check "a lock named after the qwen pid exists while the TUI runs" '[ -n "$seen" ]'
relays() { for p in $(/usr/bin/pgrep -f "qwen-relay out --sock $T/"); do tr '\0' ' ' < "/proc/$p/cmdline"; echo; done; }
rargs=""; for _ in $(seq 40); do rargs=$(relays); [ -n "$rargs" ] && break; sleep 0.1; done
check "by hand: its model relay goes first (--priority), in the shared slots folder" \
  'grep -qF -- "--slots $T/state/qwen/slots --priority" <<<"$rargs"'
wait $q; rc=$?
check "the stub TUI run exits 0" '[ "$rc" = 0 ]'
check "the lock is gone after" '[ ! -e "$T/state/qwen/locks/$q" ] && [ -z "$(ls -A "$T/state/qwen/locks")" ]'
check "the run dir is gone after" '[ -z "$(ls -d "$T/state/qwen"/run.* 2>/dev/null)" ]'
check "the TUI run started the (stub) server" '[ -e "$T/ollama-started" ]'
(cd "$T/repo" && exec "$QWEN" >/dev/null 2>&1) & q=$!  # exec: $q is the qwen pid
for _ in $(seq 40); do [ -e "$T/state/qwen/locks/$q" ] && break; sleep 0.1; done
kill -TERM $q; wait $q
check "the lock is gone after SIGTERM" '[ ! -e "$T/state/qwen/locks/$q" ]'
(cd "$T/repo" && QWEN_ROOT=$T/repo exec "$QWEN" >/dev/null 2>&1) & q=$!  # as mates runs a pane
rargs=""; for _ in $(seq 40); do rargs=$(relays); [ -n "$rargs" ] && break; sleep 0.1; done
kill -TERM $q; wait $q
check "a mates pane: its model relay shares the slots folder as a follower (no --priority)" \
  'grep -qF -- "--slots $T/state/qwen/slots" <<<"$rargs" && ! grep -qF -- "--priority" <<<"$rargs"'

# --- OpenCode config ------------------------------------------------------
# Panes share one slot, so a request can queue behind another pane's 10-minute reply; OpenCode's
# default 5-minute header timeout dropped such requests (2026-11-11 11:24 and 11:33).
printf '#!/bin/sh\nprintf %%s "$OPENCODE_CONFIG_CONTENT" > ./oc-config.json\ncat "$OPENCODE_TUI_CONFIG" > ./oc-tui.json\ncat /opt/qwen-tui/no-logo.js > ./oc-nologo.js\n' > "$T/opencode"
(cd "$T/repo" && exec "$QWEN" >/dev/null 2>&1)
ed=$(python3 -c 'import json,sys; print(*sorted(json.load(open(sys.argv[1]))["permission"]["external_directory"]))' "$T/repo/oc-config.json" 2>/dev/null)
check "no runtime: the config parses and allows only /tmp and the proofs" '[ "$ed" = "* /opt/proofs/* /tmp/*" ]'
(cd "$T/repo" && QWEN_RUNTIME=$RT exec "$QWEN" >/dev/null 2>&1)
ed=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["permission"]["external_directory"].get(sys.argv[2] + "/*"))' "$T/repo/oc-config.json" "$(readlink -f "$RT")" 2>/dev/null)
# Some leads keep proofs in the runtime (spire, 2026-11-12): OpenCode's read tool and its bash
# both refused them until the read-only runtime got an allow rule.
check "OpenCode tools may read the runtime folder" '[ "$ed" = allow ]'
ht=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["provider"]["ollama"]["options"].get("headerTimeout"))' "$T/repo/oc-config.json" 2>/dev/null)
check "OpenCode waits up to an hour for a queued reply (headerTimeout 3600000)" '[ "$ht" = 3600000 ]'
# grep and glob run the system ripgrep; without one OpenCode downloads it, which fails with no
# network (every call a wasted turn), so they are off on a host with no /usr/bin/rg.
tl=$(python3 -c 'import json,sys; t=json.load(open(sys.argv[1]))["tools"]; print(t.get("grep"), t.get("glob"))' "$T/repo/oc-config.json" 2>/dev/null)
if [ -x /usr/bin/rg ]; then want="None None"; else want="False False"; fi
check "OpenCode grep and glob tools follow the host's /usr/bin/rg ($want)" '[ "$tl" = "$want" ]'
# Wai, 2026-11-12: no big OpenCode logo on a fresh pane. A TUI plugin empties the logo slot.
tp=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plugin"])' "$T/repo/oc-tui.json" 2>/dev/null)
check "the TUI config loads the no-logo and brief plugins" '[ "$tp" = "['\''file:///opt/qwen-tui/no-logo.js'\'', '\''file:///opt/qwen-tui/brief.js'\'']" ]'
check "the no-logo plugin is readable in the sandbox" 'grep -q home_logo "$T/repo/oc-nologo.js"'
# No OpenCode prompt may wait on Wai (2026-11-11: "should ask claude instead of me"): every "ask"
# becomes "deny", and Qwen asks the lead. The real OpenCode resolves the rules (last match wins).
# The lead's proofs (/opt/proofs, read-only) must be readable by OpenCode's tools, not only by bash.
# shellcheck disable=SC2012  # a version sort of fixed names
REAL_OC=$(ls -d "$HOME"/.local/opt/opencode-*/opencode 2>/dev/null | sort -V | tail -1)
if [ -n "$REAL_OC" ] && [ -s "$T/repo/oc-config.json" ]; then
  mkdir -p "$T/ochome"
  rules=$(cd "$T/repo" && timeout 60 env -i HOME="$T/ochome" PATH=/usr/bin:/bin TERM=xterm \
    OPENCODE_DISABLE_AUTOUPDATE=1 OPENCODE_DISABLE_MODELS_FETCH=1 OPENCODE_DISABLE_LSP_DOWNLOAD=1 \
    OPENCODE_DISABLE_DEFAULT_PLUGINS=1 OPENCODE_DISABLE_CLAUDE_CODE=1 \
    OPENCODE_CONFIG_CONTENT="$(cat "$T/repo/oc-config.json")" "$REAL_OC" debug agent build 2>/dev/null)
  verdicts=$(python3 -c '
import fnmatch, json, sys
t = sys.stdin.read(); rules = json.loads(t[t.find("{"):])["permission"]
def act(perm, res):
    hit = [r for r in rules if fnmatch.fnmatchcase(perm, r["permission"]) and fnmatch.fnmatchcase(res, r["pattern"])]
    return hit[-1]["action"] if hit else "ask"
for perm, res in [("external_directory", "/home/qwen/.config/opencode/skills"), ("doom_loop", "*"),
                  ("read", "/w/.env"), ("read", "/w/.env.local"), ("question", "*"), ("plan_enter", "*"),
                  ("external_directory", "/tmp/x"), ("external_directory", "/opt/proofs/sub/*"), ("external_directory", sys.argv[1] + "/proofs/*"), ("read", "/w/.env.example"), ("bash", "ls"), ("edit", "/w/a")]:
    print(perm, res, act(perm, res))
print("asks:", sum(r["action"] == "ask" and act(r["permission"], r["pattern"]) == "ask" for r in rules))
' "$(readlink -f "$RT")" <<<"$rules" 2>&1)
  for v in "external_directory /home/qwen/.config/opencode/skills deny" "doom_loop * deny" "read /w/.env deny" \
           "read /w/.env.local deny" "question * deny" "plan_enter * deny" "external_directory /tmp/x allow" \
           "external_directory /opt/proofs/sub/* allow" \
           "external_directory $(readlink -f "$RT")/proofs/* allow" \
           "read /w/.env.example allow" "bash ls allow" "edit /w/a allow" "asks: 0"; do
    check "OpenCode permission: $v" 'grep -qxF "$v" <<<"$verdicts"'
  done
else
  echo "SKIP OpenCode permission rules: no real OpenCode in ~/.local/opt"
fi
rm -f "$T/repo/oc-config.json"
printf '#!/bin/sh\nsleep 2\n' > "$T/opencode"

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

# --- one server, however many start at once (I2) ------------------------
pkill -f "$T/stub/ollama"; sleep 0.2
S2=$T/i2; mkdir -p "$S2/stub"; cp "$T/stub/curl" "$S2/stub/"
printf '#!/bin/sh\ncat "%s/pids" 2>/dev/null\n' "$S2" > "$S2/stub/pgrep"
printf '#!/bin/sh\necho $$ >> "%s/pids"; echo started >> "%s/starts"; sleep 4\n' "$S2" "$S2" > "$S2/stub/ollama"
printf '#!/bin/sh\nsleep 1\n' > "$S2/stub/systemctl"  # hosaka's stop is slow: the race window
chmod +x "$S2/stub/"*
PATH="$S2/stub:$PATH" "$QWEN" server > "$S2/a" 2>/dev/null & pa=$!
PATH="$S2/stub:$PATH" "$QWEN" server > "$S2/b" 2>/dev/null & pb=$!
wait $pa $pb
check "two qwen server calls at once start one ollama serve" '[ "$(wc -l < "$S2/starts")" = 1 ]'
check "both callers get the same server" '[ -s "$S2/a" ] && [ "$(cat "$S2/a")" = "$(cat "$S2/b")" ]'
pkill -f "$S2/stub/ollama"; sleep 0.2

# --- the brief for OpenCode's side panel (QWEN_BRIEF): read-only in a pane, absent in a check ---
BF=$(mktemp -d "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/qwen-test-bf.XXXXXX")
echo "brief-ok" > "$BF/brief.md"
stb=$(QWEN_BRIEF=$BF QWEN_GIT_RO=1 in_ "$T/wt" "$QWEN" sandbox-test 2>/dev/null)
check "pane: the brief is readable" 'grep -qF "brief read: REACHABLE" <<<"$stb"'
check "pane: the brief cannot be changed" 'grep -qF "brief write: blocked" <<<"$stb"'
out=$(QWEN_BRIEF=$BF in_ "$T/repo" "$QWEN" exec -- sh -c '[ -e /opt/mates-brief ] && echo yes || echo no' 2>/dev/null)
check "check: no brief mounted" '[ "$out" = no ]'
QWEN_BRIEF=$T/repo in_ "$T/repo" "$QWEN" sandbox-test >/dev/null 2>&1; rc=$?
check "a brief folder that holds the repo is refused" '[ "$rc" != 0 ]'
rm -rf "$BF"

# --- sandbox-test lines -------------------------------------------------
st=$(QWEN_GIT_RO=1 in_ "$T/wt" "$QWEN" sandbox-test 2>/dev/null)
for l in "git ref write: blocked" "git object write: blocked" "worktree .git file edit: blocked" \
         "other repos' OpenCode state: blocked" "git status: REACHABLE" "your home (~/.ssh): blocked"; do
  check "sandbox-test (RO, worktree): $l" 'grep -qF "$l" <<<"$st"'
done
check "sandbox-test: the clipboard relay still works (write reached pbcopy)" 'grep -qF "clipboard write: REACHABLE" <<<"$st" && grep -qx "qwen sandbox-test" "$T/pbcopy.out"'
check "sandbox-test: clipboard read stays blocked" 'grep -qF "clipboard read: blocked" <<<"$st"'
check "sandbox-test left no probe files in git" '[ -z "$(find "$T/repo/.git" -name '\''qwen-probe*'\'')" ]'

# --- run by hand: only a git repo under ~/src (Wai, 2026-11-18: "only allow subfolders under ~/src,
# otherwise refusal is correct"; "qwen should not be running in ~/src directly, only subfolders
# that are git repos") ---------------------------------------------------------------------------
mkdir -p "$T/plain/deep"; echo x > "$T/plain/deep/f.txt"
( cd "$T/plain/deep" && "$QWEN" exec -- true ) 2>"$T/plain.err"; rc=$?
check "by hand, a folder under ~/src in no repo: refused" '[ "$rc" != 0 ] && grep -q "run qwen inside a git repo under" "$T/plain.err"'
( cd "$T/repo/sub" && "$QWEN" exec -- cat ../a.txt ) > "$T/sub.out" 2>&1; rc=$?
check "by hand, a subfolder of a repo under ~/src: the repo is mounted" '[ "$rc" = 0 ] && grep -qx hi "$T/sub.out"'
( cd "$T" && "$QWEN" exec -- true ) 2>"$T/src.err"; rc=$?
check "by hand, ~/src itself: refused" '[ "$rc" != 0 ] && grep -q "run qwen from a project under" "$T/src.err"'
O=$(mktemp -d); mkrepo "$O/r"
( cd "$O/r" && "$QWEN" exec -- true ) 2>"$T/out.err"; rc=$?; rm -rf "$O"
check "by hand, a repo outside ~/src: refused" '[ "$rc" != 0 ] && grep -q "run qwen from a project under" "$T/out.err"'
H2=$T/h2; mkrepo "$H2"; mkdir -p "$H2/src/proj"; mkrepo "$H2/src/repo"
( cd "$H2/src/proj" && HOME=$H2 QWEN_SRC=$H2/src "$QWEN" exec -- true ) 2>"$T/h2.err"; rc=$?
check "by hand, a folder in ~/src with no repo, \$HOME a repo: refused, \$HOME never found" \
  '[ "$rc" != 0 ] && grep -q "run qwen inside a git repo under" "$T/h2.err"'
( cd "$H2/src/repo" && HOME=$H2 QWEN_SRC=$H2/src "$QWEN" exec -- cat a.txt ) > "$T/h2.out" 2>&1; rc=$?
check "by hand, a repo in ~/src, \$HOME a repo: mounted" '[ "$rc" = 0 ] && grep -qx hi "$T/h2.out"'
( cd "$H2" && HOME=$H2 QWEN_SRC=$H2/src "$QWEN" exec -- true ) 2>/dev/null; rc=$?
check "by hand, \$HOME itself: refused" '[ "$rc" != 0 ]'

# --- the hide list: personal data inside a checkout never enters the sandbox ---------------
check "hide list: made with emet's data/ in it" 'grep -qx "~/src/emet/data" "$T/config/qwen/hide"'
mkdir -p "$T/repo/private"; echo SECRET > "$T/repo/private/log.ndjson"; echo SECRET > "$T/repo/notes.txt"
printf '%s\n' "$T/repo/private" "$T/repo/notes.txt" "$T/other/a.txt" >> "$T/config/qwen/hide"
( cd "$T/repo" && "$QWEN" exec -- sh -c 'cat private/log.ndjson notes.txt; ls private' ) > "$T/hide.out" 2>&1
check "hide list: a listed folder and file in the repo are empty inside" '! grep -q SECRET "$T/hide.out"'
check "hide list: still there on the host" 'grep -qx SECRET "$T/repo/private/log.ndjson"'
( cd "$T/repo" && "$QWEN" exec -- cat a.txt ) > "$T/hide2.out" 2>&1; rc=$?
check "hide list: the rest of the repo is untouched" '[ "$rc" = 0 ] && grep -qx hi "$T/hide2.out"'
rm -rf "$T/repo/private" "$T/repo/notes.txt"

# --- run by hand: the web (Wai, 2026-11-18: "when I start qwen on my own, it should have network
# access"), through the egress proxy only; mates (QWEN_ROOT) and exec stay offline --------------
hostnet=$(readlink /proc/self/ns/net)
echo 'TOKEN=s3cret' > "$T/repo/.env"; mkdir -p "$T/repo/sub/k"; echo 'PRIVATE' > "$T/repo/sub/k/id_ed25519"
echo 'keep' > "$T/repo/.env.example"
git -C "$T/repo" remote add origin https://user:tok3n@example.com/r.git
probe='readlink /proc/self/ns/net; echo "web=${QWEN_WEB:-} proxy=${HTTPS_PROXY:-} relay=${QWEN_NO_RELAY:-}"
  (exec 3<>/dev/tcp/127.0.0.1/3128) 2>/dev/null && echo PROXY-LISTENING
  (exec 3<>/dev/tcp/127.0.0.1/11434) 2>/dev/null && echo MODEL-LISTENING
  echo "env=[$(cat .env)] key=[$(cat sub/k/id_ed25519)] example=[$(cat .env.example)]"
  git config --get remote.origin.url'
nsby=$(in_ "$T/repo" "$QWEN" shell -c "$probe" 2>/dev/null)
check "by hand, shell: still its own network (not the host's)" '[ -n "$nsby" ] && ! grep -qxF "$hostnet" <<<"$nsby"'
check "by hand, shell: the web proxy on 3128, the model relay on 11434" \
  'grep -qx "web=1 proxy=http://127.0.0.1:3128 relay=" <<<"$nsby" && grep -qx PROXY-LISTENING <<<"$nsby" && grep -qx MODEL-LISTENING <<<"$nsby"'
check "by hand, shell: .env and key files read empty, .env.example kept" 'grep -qxF "env=[] key=[] example=[keep]" <<<"$nsby"'
check "by hand, shell: git remote credentials blanked" 'grep -qx "https://example.com/r.git" <<<"$nsby"'
check "by hand: the .env on disk is untouched" 'grep -qx TOKEN=s3cret "$T/repo/.env"'
check "by hand: each launch is logged in web.log" 'grep -q "^== .* $T/repo$" "$T/state/qwen/web.log"'
nsm=$(QWEN_ROOT=$T/repo in_ "$T/repo" "$QWEN" shell -c "$probe" 2>/dev/null)
check "mates (QWEN_ROOT), shell: no web, secrets as they are" \
  'grep -qx "web= proxy= relay=" <<<"$nsm" && ! grep -qx PROXY-LISTENING <<<"$nsm" && grep -qxF "env=[TOKEN=s3cret] key=[PRIVATE] example=[keep]" <<<"$nsm"'
nse=$(in_ "$T/repo" "$QWEN" exec -- bash -c "$probe" 2>/dev/null)
check "by hand, exec: no web" 'grep -q "^web= proxy= relay=1$" <<<"$nse" && ! grep -qx PROXY-LISTENING <<<"$nse"'
sth=$(in_ "$T/repo" "$QWEN" sandbox-test 2>/dev/null)
check "by hand, sandbox-test: home blocked, raw internet blocked, host loopback via proxy blocked" \
  'grep -qF "your home (~/.ssh): blocked" <<<"$sth" && grep -qF "internet 1.1.1.1:443: blocked" <<<"$sth" && grep -qF "host loopback via proxy: blocked" <<<"$sth"'
git -C "$T/repo" remote remove origin; rm -rf "$T/repo/.env" "$T/repo/.env.example" "$T/repo/sub/k"

[ $fail = 0 ] && echo "ALL PASS" || echo "SOME FAILED"
exit $fail
