# Environment
- No internet: installs, git fetch/pull/push, curl and web fetch all fail. Don't try.
- Never install anything. Need a tool, package, web page or docs, or a tool call was refused: don't work around it; ask for it by name (in `.mates/question.md` if the task is `[from Claude]`), then stop. Run the tests before saying done.
- Only this repo is visible. Shell and edits inside it are allowed.
- Search with the grep and glob tools, or bash `grep -rn` and `find`.
- A long run (test suite, smoke, browser test): give the bash tool `timeout: 600000`. The default 2 minutes kills it.
- Never write a quote, approval or decision by Wai or anyone that is not word for word in the brief or the repo. Docs that need one you lack: ask.
- Instructions: `~/.qwen/global.md` (user rules, digest), `~/.qwen/repo.md` (digest of the repo's `CLAUDE.md`; the full file is in the repo root).

# Messages starting `[from Claude]`
- Done: write `.mates/done.md`, two lines, then stop:
  1. what changed (the files);
  2. the test status, one of `passed`, `failed: <why>`, `could not run: <why>`.
- Done means the message's `Check:` command passes. Run it exactly as written (bash `timeout: 600000`) before writing done.md, and put its result on line 2. No `Check:` line: run the repo's full test command.
- Keep your plan as a todo list (the todo tool) and tick items off as you go: it shows beside the chat.
- Unsure or stuck: write the question to `.mates/question.md`, then stop.
- `.mates/` is in the repo root, the folder you start in. Write its files by full path, never `/mates/`.
- Don't commit. Git may be read-only here: `git status` and `git diff` work; add, commit, checkout and branch do not.
Other messages are from Wai: answer in chat.
