## Lessons

### What goes to Qwen

- Qwen misses what no check pins. For look-only work pin the asked values ('nothing else moved' lets wrong ones pass). An untested file in a brief comes back wrong, and its own fakes can hide real crashes: leave untested parts out or add a test that loads the real file.
- When a repo's recent Qwen tasks keep escalating, find the shared cause (often a full suite the pane does not run whole) before sending that repo more; until then do its work yourself or by follower.
- Fixes a linter or formatter can apply in place are a small task: run the tool yourself and read the result. A brief, a long suite run and a hand-off cost more than the edits.

### Checks

- Keep other queued tasks' red tests off main, or no full-suite check can pass. Keep a check from modifying the tree it judges, and try it on a known-bad tree.
- Before sending a contract that changes behaviour, run the whole suite against a rough sketch of the change, fix the old tests it breaks yourself, and settle every value (defaults, URLs, key presence); otherwise Qwen stops to ask and a wrong lead check costs a send-back.
- In a refactor, comments move with their code; fix any that name an old file, function or line, and drop none silently.
- When a change moves files, fixtures or data that another suite reads (end-to-end, browser), put that suite in the check; a unit-only check passed while 7 end-to-end tests broke and the lead's fix cost four times the hand-off.

### Writing briefs

- Keep briefs for small checkable fixes lean: behaviour and check. Name every file the task may edit; allowing edits by pattern floods the flag for unnamed files, so it says nothing.
- Size each task to fit Qwen's 64K context: one plan task, a few functions, plan sections by pointer, line ranges or grep patterns into big files. Each compaction loses detail.
- Keep each brief in a file that survives a restart and send it once, whole, into a fresh chat; leftover context from the last task or a second paste leaves Qwen re-reading.
- When Qwen writes docs or history, write the quotes, dates and numbers yourself; forbid author or credit lines in code and commits, and read those diffs line by line.
- A brief spanning a new module plus its wiring in several callers comes back with only the checked part done. Give every numbered step its own check case before the send, or split it into one send per checked piece.

### Review, merge and logging

- Probe every accept against the spec; if the check fails, send it back or finish it yourself and log that.
- After an escalation, find the cause (setup, check, brief or infrastructure) before re-sending, and keep the retry brief minimal.

### Running the panes

- Before sending, confirm the pane has the runtime at the version the check needs and can open the proof files it reads; a missing runtime or mount looks like a worker stall. After a pane restart, confirm the task resumed before waiting on it.
- Wait on Qwen with long blocking waits, not frequent polls: lead cache reads grow with every turn spent checking a slow pane and were most of the lead's cost.

### Claude followers

- Log a follower once, when its work is checked and merged, with its model in the summary; a row logged at launch reads as accepted while it still runs and counts it twice.
- When a follower writes its tests after its code, treat them as a claim: break each key guard by hand and see a test go red before merging.
