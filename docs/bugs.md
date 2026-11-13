# Known mates bugs

Open bugs in `mates` (the lead/Qwen pane launcher), newest first. A bug that gets
fixed leaves this list; its fix commit says what changed.

## 2026-11-13, from the human-music and spire leads

### `mates wait` says idle on one missed screen read

On a shared slot each Qwen step waits ~10 minutes, so the database is quiet past
STALE_S between steps and only the spinner on screen says busy. One capture that
misses it (OpenCode mid-redraw) is enough: human-music's `mates wait 2` printed
`idle` while `mates check 2` saw the pane busy a moment later (pane %8, task
15df4cfe8f80).

- **Wanted:** idle only when it holds on two polls in a row. A red test is
  ready in the lead's scratchpad; it lands after the screen-first busy work merges.

### OpenCode loops on "no user query found in messages"; wait says idle

spire pane 2 (task b46d4ea614f6) made seven normal tool calls from a 3,199-char
plain-text brief, then OpenCode looped "no user query found in messages
[retrying in 19s ...]". The error is Ollama's ("chat prompt error", serve.log
2026-11-13 03:47:20) while building the prompt; the prompt was about 47K of the
65K window and OpenCode's last write was an empty assistant message, so the
failing request may be OpenCode's compaction call. Not truncation: the log's one
"truncating input prompt" is from 11-12 21:10. Not `answer` versus `send`: the
brief reached OpenCode as an ordinary text part.

- **Wanted:** `mates wait` reports an error the screen shows ("retrying in",
  "error") as `error: <line>` instead of `idle`, so the lead acts at once.
- **To learn:** what request fails, by logging the request OpenCode sends on
  the next occurrence.

## 2026-11-12, from the mates lead

### discard leaves a working Qwen running on the reset worktree

`mates discard N` resets the worktree to the lead's HEAD but does not stop
OpenCode. A Qwen still in its turn goes on editing the reset tree from its old
context, and the next `send` reads the pane as busy. One Escape does not stop it:
OpenCode needs two presses (the first only arms the interrupt).

- **Wanted:** discard interrupts a busy pane first (two Escapes, then wait for
  the spinner to go) or refuses while it is busy.
- **Worse with `/new`:** `/new` on a busy pane opens a fresh chat but leaves the
  old one running out of sight. Here it kept working the discarded task for three
  hours, edited the reset worktree, and kept the pane reading busy (its writes
  are the newest in the database). Only restarting the pane stopped it. mates'
  own `send` pastes `/new` only on an idle pane, so it is safe; a lead's hand
  `/new` on a busy pane is not.

## 2026-11-12, from the spire lead after the WSL 3.0.5 restart

### send refuses a fresh pane while a killed pane's reply looks live

spire killed pane %9 mid-reply; `mates add` opened %10. `mates send 1` then
refused with "Qwen pane 1 is busy (OpenCode is replying)" although %10 sat idle
on OpenCode's empty start screen. The brief was pasted by hand, so it never
reached the task log.

- **Cause (from the code):** `_busy()` (`mates/cli.py:427`) asks
  `opencode_session()` for the worktree's newest OpenCode session. A fresh
  pane has none until its first message, so the newest one is the killed
  pane's, whose last reply never finished. `opencode_state()` calls that busy
  until nothing in it changes for STALE_S (300 s).
- **Workaround:** wait 5 minutes after the kill, then send.
- **Wanted:** a reply whose OpenCode process is gone is not busy. For example,
  only count a session the live pane is showing, or treat OpenCode's empty
  start screen as idle.

## 2026-11-12, from the 00:30 WSL crash

### Two Qwen panes in one repo race for OpenCode's database at restart

After the 00:30 crash, spire's two Qwen panes restarted within a second of each
other (00:48:04). Pane 2 came up. Pane 1 printed `Error: database is locked`,
`qwen` exited 1, and mates marked the pane failed. Someone restarted it at
00:49:25 as a fresh `opencode` with no `-s`, so it lost its old session.
human-music's two panes started the same way a second apart and both came up.

- **Likely cause:** all of a repo's worktrees share one OpenCode state folder
  (`qwen opencode-dir`, so one `opencode.db`). The failing run never wrote its
  "creating instance" log line, so it died opening the database while pane 2
  was writing to it (init plus "project copy refresh", 05:48:05–07Z in that
  folder's `log/opencode.log`). Not confirmed: three racing
  `opencode session list` runs on a fresh DB (24 tries) and on a copy of
  spire's DB and WAL (18 tries) never hit the lock, so the TUI start path is
  the suspect.
- **Wanted:** a lock error at start should not fail the pane. Either mates
  starts one repo's panes one after another, or `qwen` retries a start that
  fails with "database is locked". And a restart should keep the session id.
- **Stale session id after the hand restart:** `mates list` still shows pane
  1's pre-restart session (`ses_e31c95d2cffeQX26zVlwDjYAMU`), so
  `mates send --continue 1` would go to the wrong session. The open task was
  re-sent to the new pane as attempt 2 and is running.

## 2026-11-11, from the YTM mates lead

### A pane with an open task comes back idle after a WSL restart

After the 11:36 WSL crash, the pane holding YTM's open task came back (pane %7
became %4) showing the old transcript and generating nothing. The reply in
flight died with the server. mates still listed the task as open, under a new
OpenCode session id, and has no resend or resume. With the input-box bug
blocking `answer`, the only way out was discard, `/new`, then send again.

- **Wanted:** on launch, find tasks whose pane has no reply running and no
  finished reply, and say so. Offer a logged resend (same family as the
  dirty-worktree re-brief below).

## 2026-11-11, from the spire mates lead

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
- **Hit again** in spire after the 2026-11-12 00:30 crash; re-set by hand.
- **Hit a third time** after the WSL 3.0.5 restart. The hand re-set itself
  failed first: it imported `session.py` from an old path, so pane 1 restarted
  with no runtime. `mates/session.py` has never moved in git, so the old path
  was likely another checkout. A runtime built from `.mates/runtime.sh` with
  `mates runtime` comes back on restart; one set by hand does not.

### No way to re-brief a pane with a dirty worktree

mates has no way to send a pane back its work or give it a new brief when its
worktree is dirty. `mates send` refuses a dirty worktree, and `mates answer`
hits the input-box bug above. A pane restarted to pick up a new runtime can only
be re-briefed by pasting into tmux by hand, and that paste never reaches the
task log.

- **Wanted:** a supported, logged way to paste into a pane whose worktree has
  work in progress (for example `mates send --continue`, or `answer` once the
  input-box bug is fixed).
