# Mates Task 0 probe (2026-11-10)

Plain `qwen` (OpenCode 1.19.5, `qwen3.8-27b-turbo-heretic:mtp-iq4xs`, 64K) in a
detached tmux pane, scratch git repo.

| Probe | Result |
|---|---|
| Paste | **Works.** `tmux load-buffer` + `paste-buffer -p` (bracketed) + `send-keys Enter` delivers a 3-line message as one message. |
| Prompt size | OpenCode's first request was 7,966 tokens (system prompt + tools + our instructions), read in 14 s at ~570 tok/s. Claude Code's was ~12K. |
| Offline | OpenCode's Ollama connector is bundled; no download needed in the sandbox. |
| Busy signal | `opencode.db` `message.data` → `time.completed` is set when an assistant reply ends. An interrupted reply also gets it. |
| Protocol, clear task | **Works.** `[from Claude]` bug fix: Qwen fixed it, wrote `.mates/done.md` ("Fixed add() in calc.py (changed subtraction to addition); test_calc.py prints ok."), did not commit. 145 s. |
| Protocol, ambiguous task | **Fails.** "[from Claude] Rename the helper to the better name in calc.py." Qwen did not write `question.md`: it guessed (`add` → `add_numbers`) and edited the test to match. Consequences: Claude's briefs must name exact symbols and the check; `mates diff`/merge review treats any test-file change as a flag; do not rely on Qwen asking. |
| `/new` | Clears the conversation, but the new session appears in `opencode.db` only after its first message. Look the session up after `send`, not after `/new`. |
| Restore | **Works.** `qwen -s <session id>` reopens the same conversation (earlier edits and test runs shown). |
| Claude title after `/clear` | Not probed (needs an interactive Claude session). `lead.md` tells the lead to `/rename "mates <repo>"` after a clear, as the plan's fallback. |
| Two panes at once | Not re-measured. Phase 1 measured two concurrent workers on a dense 27B model at 0.4-0.5 tok/s each; the current model is dense too. `"parallel"` stays false: one open task at a time. |
