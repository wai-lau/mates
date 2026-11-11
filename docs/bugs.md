# Known mates bugs

Open bugs in `mates` (the lead/Qwen pane launcher), newest first. A bug that gets
fixed leaves this list; its fix commit says what changed.

## 2026-11-11, from the spire mates lead

### send/answer refuse a used OpenCode pane

`mates send` and `mates answer` refuse with "Qwen pane N has text in its input
box (Wai may be typing); not pasting over it" on any pane whose OpenCode session
has already been used, even when the box is empty.

- **Cause:** `input_empty()` (`mates/cli.py:433`) only looks for `INPUT_EMPTY`
  (`mates/cli.py:355`, `Ask anything… "`), which is OpenCode's placeholder. A
  session that has been used no longer shows it, so an empty box reads as "has
  text".
- **Repro:** have Qwen finish one task in a pane, then `mates send <n> ...` with
  the input box empty.
- **Workaround:** paste `/new` by hand. That brings the placeholder back but
  throws away the chat.

### `mates wait` reads idle mid-task

`mates wait` (and `send`/`check`, which share the rule) says a pane is `idle` in
the middle of a task when nothing in its OpenCode session changes for more than
`STALE_S` (300 s, `mates/session.py:27`). In spire, a 14m53s turn read as idle
several times.

- **Cause:** `opencode_state()` (`mates/session.py:222`) treats "no message or
  part updated for STALE_S" as a crashed OpenCode. Nothing gets written while a
  request waits for the server's one slot, which is shared by every pane (or
  while a long think writes nothing), so a live request looks dead.
- **Made more likely by f31922e:** OpenCode used to drop a request after 5
  minutes without reply headers; it now waits up to an hour, and the wait writes
  nothing.
- **Two possible fixes:** ask the private server whether a request for this
  pane is queued or running, or raise STALE_S to match.

### A restart drops a runtime set by hand

Restarting mates resets the saved `runtime` to None, so a runtime the lead set
by hand disappears without a warning. After that, checks run without it (in
spire, without `$SPIRE_PROOFS` and the browser-test tools).

- **Cause:** `launch_plan()` (`mates/cli.py:165`) always sets `st["runtime"]`
  from the runtime built for HEAD's `.mates/runtime.sh`, or None when there is
  none. A runtime already in the saved state is never kept.
- **To decide:** keep a runtime the saved state already has when no
  `.mates/runtime.sh` is built (and warn), or make `.mates/runtime.sh` plus
  `mates runtime` the only supported way and warn when a restart drops a
  runtime.

### No way to re-brief a pane with a dirty worktree

mates has no way to send a pane back its work or give it a new brief when its
worktree is dirty. `mates send` refuses a dirty worktree, and `mates answer`
hits the input-box bug above. A pane restarted to pick up a new runtime can only
be re-briefed by pasting into tmux by hand, and that paste never reaches the
task log.

- **Wanted:** a supported, logged way to paste into a pane whose worktree has
  work in progress (for example `mates send --continue`, or `answer` once the
  input-box bug is fixed).
