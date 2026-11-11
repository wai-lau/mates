#!/usr/bin/env bash
# bin/mates: window choice, restore, failure shells. A private tmux server (-L), stub
# claude and qwen (MATES_CLAUDE, MATES_QWEN), HOME a temp dir. No model, no real claude.
#   bash tests/test_launcher.sh
set -uo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
MATES=$HERE/bin/mates
command -v tmux >/dev/null || { echo "SKIP: tmux not installed"; exit 0; }

T=$(mktemp -d) || exit 1
T=$(cd "$T" && pwd -P) || exit 1  # the real path: git and tmux report it
SOCK=mates-test-$$
tx() { tmux -L "$SOCK" -f /dev/null "$@"; }
cleanup() { tx kill-server 2>/dev/null; rm -rf "$T"; }
trap cleanup EXIT
unset TMUX TMUX_PANE MATES_LEAD
mkdir -p "$T/home" "$T/ctl" "$T/oc"
export HOME=$T/home SHELL=/bin/sh MATES_CLAUDE=$T/claude MATES_QWEN=$T/qwen
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

# claude: logs "<repo> <session args>"; with --session-id writes a transcript titled by -n;
# runs until ctl/<repo>.quit exists, then exits with the code in it.
cat > "$T/claude" <<EOF
#!/bin/sh
b=\$(basename "\$PWD"); sid= title= resume=
while [ \$# -gt 0 ]; do
  case \$1 in --session-id) sid=\$2; shift ;; -n) title=\$2; shift ;; --resume) resume=\$2; shift ;; esac
  shift
done
echo "\$b lead=\$MATES_LEAD root=\$MATES_ROOT key=\${ANTHROPIC_API_KEY-unset} sid=\$sid resume=\$resume" >> '$T/claude.log'
if [ -n "\$sid" ]; then
  d="\$HOME/.claude/projects/\$(printf %s "\$PWD" | sed 's/[^A-Za-z0-9]/-/g')"; mkdir -p "\$d"
  printf '{"type":"custom-title","customTitle":"%s"}\n' "\$title" > "\$d/\$sid.jsonl"
fi
while [ ! -e '$T/ctl/'"\$b.quit" ]; do sleep 0.1; done
rc=\$(cat '$T/ctl/'"\$b.quit"); rm -f '$T/ctl/'"\$b.quit"; exit "\$rc"
EOF
# qwen: warm and opencode-dir answer at once; a run logs and sleeps, or exits 1 if ctl/<repo>.qwen-fail.
cat > "$T/qwen" <<EOF
#!/bin/sh
case \$1 in warm) exit 0 ;; opencode-dir) echo '$T/oc'; exit 0 ;; esac
b=\$(basename "\$(dirname "\$(dirname "\$(dirname "\$PWD")")")")
echo "\$b root=\$QWEN_ROOT ro=\$QWEN_GIT_RO pwd=\$PWD args=\$*" >> '$T/qwen.log'
[ -e '$T/ctl/'"\$b.qwen-fail" ] && { echo "qwen: no model" >&2; exit 1; }
exec sleep 600
EOF
chmod +x "$T/claude" "$T/qwen"

mkrepo() { git init -q "$T/$1" && echo hi > "$T/$1/a.txt" && git -C "$T/$1" add a.txt && git -C "$T/$1" commit -qm init; }
key() { printf %s "$T/$1" | sed 's/[^A-Za-z0-9]/-/g'; }
count() { grep -c "^$1 " "$T/$2.log" 2>/dev/null || true; }
tagged() { tx list-panes -a -F $'#{pane_id}\t#{@mates}' | awk -F'\t' -v k="$1" '$2 == k { print $1 }'; }
mates_windows() { tx list-windows -a -F $'#{window_id}\t#{@mates-window}' | awk -F'\t' -v k="$1" '$2 == k { print $1 }'; }
waitfor() { local i; for i in $(seq 100); do eval "$1" && return 0; sleep 0.1; done; return 1; }

fail=0
ok() { echo "PASS $1"; }
bad() { echo "FAIL $1"; fail=1; }
check() { local name=$1; shift; if eval "$*"; then ok "$name"; else bad "$name"; fi; }

# the server, with a pane that keeps it up between tests
tx new-session -d -s keep -x 200 -y 50 "sleep 600"

# --- test_one_pane_window_reused ----------------------------------------------------
mkrepo r1; K1=$(key r1)
tx new-session -d -s t1 -x 200 -y 50 -c "$T/r1" "$MATES"
W1=$(tx display -p -t t1 '#{window_id}'); P1=$(tx display -p -t t1 '#{pane_id}')
waitfor '[ "$(count r1 claude)" = 1 ] && [ "$(count r1 qwen)" = 1 ]'
check "one-pane window: mates runs in it (no new window)" '[ "$(tx list-windows -t t1 | wc -l)" = 1 ]'
check "one-pane window: tagged as the mates window" '[ "$(mates_windows "$K1")" = "$W1" ]'
check "one-pane window: the lead is the original pane" '[ "$(tagged "$K1:lead")" = "$P1" ]'
check "one-pane window: one Qwen pane beside it" '[ "$(tx list-panes -t "$W1" | wc -l)" = 2 ] && [ -n "$(tagged "$K1:1")" ]'
check "lead env: MATES_LEAD=1, MATES_ROOT, no ANTHROPIC_API_KEY, new session id" \
  'grep -q "^r1 lead=1 root=$T/r1 key=unset sid=[0-9a-f-]\{36\} resume=$" "$T/claude.log"'
check "qwen pane: QWEN_ROOT is its worktree, git read-only, run there, new session" \
  'grep -qx "r1 root=$T/r1/.claude/worktrees/mates-qwen-1 ro=1 pwd=$T/r1/.claude/worktrees/mates-qwen-1 args=" "$T/qwen.log"'
check "qwen pane: marked @mates-qwen-live while qwen runs (mates send needs it)" \
  '[ "$(tx show-options -pqv -t "$(tagged "$K1:1")" @mates-qwen-live)" = 1 ]'
check "state saved with the lead's session id" \
  'python3 -c "import json,sys; s=json.load(open(sys.argv[1])); sys.exit(not (s[\"claude_session\"] and [q[\"n\"] for q in s[\"qwens\"]] == [1]))" "$HOME/.claude/mates/sessions/$K1.json"'

# --- test_two_pane_window_gets_new_window --------------------------------------------
mkrepo r2; K2=$(key r2)
tx new-session -d -s t2 -x 200 -y 50 -c "$T/r2" "sleep 600"
W2=$(tx display -p -t t2 '#{window_id}')
tx split-window -h -t "$W2" -c "$T/r2" "$MATES; exec sleep 600"
waitfor '[ "$(count r2 claude)" = 1 ]'
M2=$(mates_windows "$K2")
check "two-pane window: a new window for mates" '[ "$(tx list-windows -t t2 | wc -l)" = 2 ] && [ -n "$M2" ] && [ "$M2" != "$W2" ]'
check "two-pane window: left as it was (2 panes, untagged)" \
  '[ "$(tx list-panes -t "$W2" | wc -l)" = 2 ] && [ -z "$(tx show-options -wqv -t "$W2" @mates-window)" ]'
check "two-pane window: the new window has the lead and a Qwen pane" '[ -n "$M2" ] && [ "$(tx list-panes -t "$M2" | wc -l)" = 2 ]'

# --- test_second_mates_switches_to_first ---------------------------------------------
tx new-session -d -s t3 -x 200 -y 50 -c "$T/r1" "$MATES > '$T/t3.out' 2>&1; echo \$? > '$T/t3.rc'; exec sleep 600"
waitfor '[ -s "$T/t3.rc" ]'
check "second mates: exits 0" '[ "$(cat "$T/t3.rc")" = 0 ]'
check "second mates: says it is already running" 'grep -q "already running" "$T/t3.out"'
check "second mates: no second lead or Qwen, no split" \
  '[ "$(count r1 claude)" = 1 ] && [ "$(count r1 qwen)" = 1 ] && [ "$(tx list-panes -t t3 | wc -l)" = 1 ] && [ "$(mates_windows "$K1" | wc -l)" = 1 ]'

# --- test_exited_lead_restarted -------------------------------------------------------
mkrepo r4; K4=$(key r4)
tx new-session -d -s t4 -x 200 -y 50 -c "$T/r4" "$MATES"
waitfor '[ "$(count r4 claude)" = 1 ] && [ -n "$(tagged "$K4:1")" ]'
L4=$(tagged "$K4:lead"); Q4=$(tagged "$K4:1")
SID4=$(sed -n 's/^r4 .* sid=\([0-9a-f-]*\) .*/\1/p' "$T/claude.log")
echo 1 > "$T/ctl/r4.quit"
waitfor '[ -z "$(tx show-options -pqv -t "$L4" @mates-lead-live)" ]'
tx new-session -d -s t4b -x 200 -y 50 -c "$T/r4" "$MATES; exec sleep 600"
waitfor '[ "$(count r4 claude)" = 2 ]'
check "exited lead: restarted in the same pane" '[ "$(tagged "$K4:lead")" = "$L4" ]'
check "exited lead: resumes the same conversation" 'grep -q "^r4 .* sid= resume=$SID4$" "$T/claude.log"'
check "exited lead: Qwen pane left alone (same pane, qwen started once)" \
  '[ "$(tagged "$K4:1")" = "$Q4" ] && [ "$(count r4 qwen)" = 1 ] && [ "$(tx list-panes -t "$L4" | wc -l)" = 2 ]'
check "exited lead: the lead is live again" 'waitfor "[ \"\$(tx show-options -pqv -t $L4 @mates-lead-live)\" = 1 ]"'

# claude exited 0, so its pane closed: a new lead pane opens on the left of the Qwen pane
mkrepo r7; K7=$(key r7)
tx new-session -d -s t7 -x 200 -y 50 -c "$T/r7" "$MATES"
waitfor '[ "$(count r7 claude)" = 1 ] && [ -n "$(tagged "$K7:1")" ]'
Q7=$(tagged "$K7:1"); W7=$(mates_windows "$K7")
echo 0 > "$T/ctl/r7.quit"
waitfor '[ -z "$(tagged "$K7:lead")" ]'
tx new-session -d -s t7b -x 200 -y 50 -c "$T/r7" "$MATES; exec sleep 600"
waitfor '[ "$(count r7 claude)" = 2 ]'
L7=$(tagged "$K7:lead")
check "closed lead: a new lead pane, left of the Qwen pane, in the mates window" \
  '[ -n "$L7" ] && [ "$(tx display -p -t "$L7" "#{window_id}")" = "$W7" ] && [ "$(tx display -p -t "$L7" "#{pane_left}")" -lt "$(tx display -p -t "$Q7" "#{pane_left}")" ]'
check "closed lead: the Qwen pane left alone" '[ "$(tagged "$K7:1")" = "$Q7" ] && [ "$(count r7 qwen)" = 1 ]'

# --- test_failing_claude_leaves_shell -------------------------------------------------
mkrepo r5; K5=$(key r5)
echo 3 > "$T/ctl/r5.quit"
tx new-session -d -s t5 -x 200 -y 50 -c "$T/r5" "$MATES"
P5=$(tx display -p -t t5 '#{pane_id}')
waitfor 'tx capture-pane -p -t "$P5" | grep -q "claude exited 3"'
check "failing claude: the message is shown" 'tx capture-pane -p -t "$P5" | grep -q "claude exited 3"'
check "failing claude: the pane stays, a shell in it" \
  '[ -n "$(tx display -p -t "$P5" "#{pane_id}" 2>/dev/null)" ] && [ "$(tx display -p -t "$P5" "#{pane_current_command}")" = sh ]'
check "failing claude: the lead is not marked live" '[ -z "$(tx show-options -pqv -t "$P5" @mates-lead-live)" ]'

# --- test_failed_qwen_pane_not_closed -------------------------------------------------
mkrepo r6; K6=$(key r6)
touch "$T/ctl/r6.qwen-fail"
tx new-session -d -s t6 -x 200 -y 50 -c "$T/r6" "$MATES"
waitfor '[ -n "$(tagged "$K6:1")" ] && [ "$(tx show-options -pqv -t "$(tagged "$K6:1")" @mates-failed)" = 1 ]'
Q6=$(tagged "$K6:1")
check "failed qwen: the pane stays, tagged and marked failed" \
  '[ -n "$Q6" ] && [ "$(tx show-options -pqv -t "$Q6" @mates-failed)" = 1 ]'
check "failed qwen: the error and a shell are left" \
  'waitfor "tx capture-pane -p -t $Q6 | grep -q \"qwen exited 1\"" && [ "$(tx display -p -t "$Q6" "#{pane_current_command}")" = sh ]'
check "failed qwen: the shell is in the lead repo, not the worktree, and says it is unsandboxed" \
  '[ "$(tx display -p -t "$Q6" "#{pane_current_path}")" = "$T/r6" ] && tx capture-pane -p -t "$Q6" | grep -q "OUTSIDE the sandbox"'
check "failed qwen: not marked @mates-qwen-live (send refuses the shell)" \
  '[ -z "$(tx show-options -pqv -t "$Q6" @mates-qwen-live)" ]'
check "failed qwen: the entry is not closed" \
  'python3 -c "import json,sys; s=json.load(open(sys.argv[1])); sys.exit(not [q for q in s[\"qwens\"] if q[\"n\"] == 1 and not q[\"closed\"]])" "$HOME/.claude/mates/sessions/$K6.json"'

# --- bin/mates refuses inside a Qwen worktree ------------------------------------------
QWT=$T/r1/.claude/worktrees/mates-qwen-1
out=$(cd "$QWT" && "$MATES" 2>&1); rc=$?
check "qwen worktree: launch refused" '[ "$rc" != 0 ] && grep -q "Qwen worktree" <<<"$out"'
out=$(cd "$QWT" && MATES_LEAD=1 "$MATES" list 2>&1); rc=$?
check "qwen worktree: subcommands refused" '[ "$rc" != 0 ] && grep -q "Qwen worktree" <<<"$out"'
mkdir -p "$QWT/sub"; out=$(cd "$QWT/sub" && "$MATES" 2>&1); rc=$?
check "qwen worktree: refused from a subfolder" '[ "$rc" != 0 ] && grep -q "Qwen worktree" <<<"$out"'
git -C "$T/r1" worktree add -q -b elsewhere "$T/moved/mates-qwen-9"  # admin dir named mates-qwen-9, path elsewhere
out=$(cd "$T/moved/mates-qwen-9" && "$MATES" 2>&1); rc=$?
check "qwen worktree: refused by its git admin dir name" '[ "$rc" != 0 ] && grep -q "Qwen worktree" <<<"$out"'
check "qwen worktree: nothing started" '[ "$(count r1 claude)" = 1 ] && [ "$(count r1 qwen)" = 1 ]'

[ "$fail" = 0 ] && echo "ALL PASS" || echo "SOME FAILED"
exit "$fail"
