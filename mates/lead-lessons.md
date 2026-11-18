## Lessons

### What goes to Qwen

- For look-only work pin the asked values in the check; 'nothing else moved' lets wrong ones pass.
- Qwen misses what no test pins: an untested file in a brief comes back wrong, and its own fakes can hide real crashes. Leave untested parts out or add a test that loads the real file.

### Checks

- Keep other queued tasks' red tests off main, or no full-suite check can pass. Keep a check from modifying the tree it judges, and try it on a known-bad tree.
- Before sending a contract that changes behaviour, run the whole suite against a rough sketch of the change, fix the old tests it breaks yourself, and settle every value (defaults, URLs, key presence); otherwise Qwen stops to ask and a wrong lead check costs a send-back.
- Probe-first tasks (the probe or failing tests written first, sent as the spec) passed review on the first try.
- In a refactor, comments move with their code; fix any that name an old file, function or line, and drop none silently.

### Writing briefs

- Keep briefs for small checkable fixes lean: behaviour and check. Name every file the task may edit; allowing edits by pattern floods the flag for unnamed files, so it says nothing.
- Size each task to fit Qwen's 64K context: one plan task, a few functions, plan sections by pointer, line ranges or grep patterns into big files. Each compaction loses detail.
- Keep each brief in a file that survives a restart and send it once, whole, into a fresh chat; leftover context from the last task or a second paste leaves Qwen re-reading.
- When Qwen writes docs or history, write the quotes, dates and numbers yourself; forbid author or credit lines in code and commits, and read those diffs line by line.

### Review, merge and logging

- Probe every accept against the spec; if the check fails, send it back or finish it yourself and log that.
- After an escalation, find the cause (setup, check, brief or infrastructure) before re-sending, and keep the retry brief minimal.

### Running the panes

- Before sending, confirm the pane has the runtime and can open the proof files its check needs; a missing runtime or mount looks like a worker stall.
- After a pane restart, confirm the task resumed before waiting on it.
- Wait on Qwen with long blocking waits, not frequent polls: lead cache reads grow with every turn spent checking a slow pane and were most of the lead's cost.
