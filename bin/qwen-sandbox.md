# Environment
- No internet: installs, git fetch/pull/push, curl and web fetch all fail. Don't try.
- Never install anything. Need a tool, package, web page or docs: ask for it by name (in `.mates/question.md` if the task is `[from Claude]`), then stop. Run the tests before saying done.
- Only this repo is visible. Shell and edits inside it are allowed.
- Instructions: `~/.qwen/global.md` (user rules, digest), `~/.qwen/repo.md` (digest of the repo's `CLAUDE.md`; the full file is in the repo root).

# Messages starting `[from Claude]`
- Done: write `.mates/done.md`, two lines, then stop:
  1. what changed (the files);
  2. the test status, one of `passed`, `failed: <why>`, `could not run: <why>`.
- Unsure or stuck: write the question to `.mates/question.md`, then stop.
- Don't commit. Git may be read-only here: `git status` and `git diff` work; add, commit, checkout and branch do not.
Other messages are from Wai: answer in chat.
