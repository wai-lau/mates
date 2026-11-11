# Answered questions

Decisions Wai made on open questions, newest first.

| When | Question | Wai's answer | Reading taken |
|---|---|---|---|
| 2026-11-11 | How should the lead install the tools Qwen needs to run a repo's tests (Qwen's sandbox has no uv, pytest, venv or node_modules)? | "A" | The recipe is committed in each repo as `.mates/runtime.sh`; the lead builds it mid-session with a new `mates runtime`; the install runs in its own sandbox with Wai's home and tokens hidden; idle Qwen panes restart with their chat kept; Qwen asks for a missing tool instead of writing `could not run`. Tightened after Wai's follow-up ("PII and stuff must never enter the sandbox", "Ensure that Claude doesn't just install whatever Qwen tells it to"): the build sees only the script and its named lockfiles from the commit, reaches only PyPI and npm, and the lead treats a Qwen request as a claim |
| 2026-11-11 | Push the finished mates work (77 commits here), hosaka's GPU-lock commit, and the dotfiles changes (she/her line, 5-hour pause hook)? | "A" | Push all three; dotfiles as one commit with both files |
