# Environment
- No internet: installs, git fetch/pull/push, curl and web fetch all fail. Don't try. If something is missing, say so and stop.
- Need a web page or docs: ask for it (in `.mates/question.md` if the task is `[from Claude]`), then stop.
- Only this repo is visible. Shell and edits inside it are allowed.
- Instructions: `~/.qwen/global.md` (user rules, digest), `~/.qwen/repo.md` (digest of the repo's `CLAUDE.md`; the full file is in the repo root).

# Messages starting `[from Claude]`
- Done: write a one-line summary (what changed, tests pass?) to `.mates/done.md`, then stop.
- Unsure or stuck: write the question to `.mates/question.md`, then stop.
- Don't commit.
Other messages are from Wai: answer in chat.
