# Mates Task 0 probe (2026-11-10)

Plain `qwen` (OpenCode 1.19.5, `qwen3.8-27b-turbo-heretic:mtp-iq4xs`, 64K) in a
detached tmux pane, scratch git repo.

| Probe | Result |
|---|---|
| Paste | **Works.** `tmux load-buffer` + `paste-buffer -p` (bracketed) + `send-keys Enter` delivers a 3-line message as one message. |
| Prompt size | OpenCode's first request was 7,966 tokens (system prompt + tools + our instructions), read in 14 s at ~570 tok/s. Claude Code's was ~12K. |
| Offline | OpenCode's Ollama connector is bundled; no download needed in the sandbox. |
| Busy signal | `opencode.db` `message.data` → `time.completed` is set when an assistant reply ends. An interrupted reply also gets it. A reply that uses tools is several assistant messages, each tool step completed with `finish: "tool-calls"` and the next step's row created about 15 ms later, so `opencode_state` counts a `"tool-calls"` finish as busy. |
| Protocol, clear task | **Works.** `[from Claude]` bug fix: Qwen fixed it, wrote `.mates/done.md` ("Fixed add() in calc.py (changed subtraction to addition); test_calc.py prints ok."), did not commit. 145 s. |
| Protocol, ambiguous task | **Fails.** "[from Claude] Rename the helper to the better name in calc.py." Qwen did not write `question.md`: it guessed (`add` → `add_numbers`) and edited the test to match. Consequences: Claude's briefs must name exact symbols and the check; `mates diff`/merge review treats any test-file change as a flag; do not rely on Qwen asking. **Fix, re-measured:** end every Claude message with "(If any name, file or expected result above is not stated exactly, write your question to .mates/question.md and stop before editing. Do not edit tests unless told.)". Same ambiguous task: Qwen wrote `question.md` ("What is the intended name for the helper function in calc.py?...") in 210 s and edited nothing. |
| `/new` | Clears the conversation, but the new session appears in `opencode.db` only after its first message. Look the session up after `send`, not after `/new`. |
| Restore | **Works.** `qwen -s <session id>` reopens the same conversation (earlier edits and test runs shown). |
| Claude title after `/clear` | Not probed (needs an interactive Claude session). `lead.md` tells the lead to `/rename "mates <repo>"` after a clear, as the plan's fallback. |
| Two panes at once | Not re-measured. Phase 1 measured two concurrent workers on a dense 27B model at 0.4-0.5 tok/s each; the current model is dense too. `"parallel"` stays false: one open task at a time. |

## Task 1 Step 4b: OpenCode under read-only git (2026-11-10)

In a git worktree of a scratch repo, `QWEN_GIT_RO=1 bin/qwen` (branch build `2a5f9a1`):

- `sandbox-test`: git status works; hooks, config, ref, object and worktree `.git` file writes are all blocked; clipboard write works, clipboard read is blocked.
- Toy task (`[from Claude]` + the ask-suffix): OpenCode started and edited `calc.py`, with no git error in the pane. It wrote `.mates/done.md` in the new two-line form ("calc.py: changed add() from returning a - b to a + b" / "test status: passed"). 155 s. It did not commit.
- Side effect: the test run left an untracked `__pycache__/`. In real repos `.gitignore` covers it; in a repo without one, `git add -A` in `mates check`'s snapshot would pick it up, and the guard flags it as an `unnamed-file`.
