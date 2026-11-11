# Mates Phase 2 (two-pane `mates`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `mates` in a git repo opens Claude Code on the left and one or more
sandboxed `qwen` (OpenCode) panes on the right. Each Qwen pane works in its
own worktree. Claude delegates by pasting into a Qwen pane as Wai, waits on
`.mates/done.md` or `.mates/question.md`, runs the check inside the sandbox,
reviews, and merges. It restores the previous Claude conversation and all Qwen
panes the way spire-trio does.

**Architecture:**
- **`bin/mates`** (bash) finds or opens the tmux window, and dispatches
  subcommands to `mates/cli.py`.
- **`mates/cli.py`** works on a per-repo session file and the panes. There is
  no daemon.
- **Qwen panes run `bin/qwen`,** with three small additions (Task 1): an
  `exec` mode that runs any command in the sandbox, a read-only git mode for
  worktrees, and a runtime mount.

**Tech Stack:** bash, Python 3.12 stdlib (sqlite3 to read OpenCode's session DB), tmux 3.4, Claude Code 2.1.322 (`--session-id`, `--resume`, `-n`), OpenCode 1.19.5 (`-s <session>`).

**Spec:** `docs/superpowers/specs/2026-11-10-claude-qwen-mates-design.md`. The
section "Revision 2026-11-10 (evening): two-pane mates" wins over everything
below it.

**Revised 2026-11-10 (late)** after an adversarial review. What changed:
Qwen-written code never runs as Wai before merge (`mates check`); Qwen's git
metadata is read-only; panes are found by tag, not stored id; restore no
longer erases itself; `send` pastes
instead of typing; added `discard`, a default `wait` timeout, a duplicate-run
guard, the hosaka lock, the cost fields, and a probe (Task 0) before any
build; the retro has no tools and `cli` writes its output.

**Round 2 (same night)** of the review. What changed: all of git is
read-only in mates panes, and the lead's git never finds the repo through
Qwen's folder; `check` runs on a snapshot and `merge` commits that snapshot;
`sync` when the lead branch moved; a Qwen pane that fails to start keeps its
place; OpenCode state is per repo; each task gets a new OpenCode session; the
GPU lock moved into `bin/qwen`; `--fresh` and merging Wai's own Qwen work are
defined.

## Global Constraints

- The commands are exactly `mates` and `qwen`. Plain `claude` stays untouched.
- **Fresh vs restore:**
  - First run in a repo: 1 Claude and 1 Qwen.
  - Later runs: restore the Claude conversation and every Qwen pane that was
    open (Wai, verbatim).
  - `mates --fresh` starts a new Claude conversation and one Qwen pane with
    a new OpenCode session. Worktrees and their work are kept: a dirty one is
    named in a warning, and entries above n=1 stay in the state as closed.
  - `mates [--fresh] -- <args>` passes `<args>` to `claude`.
- **Session state:** `~/.claude/mates/sessions/<repo-key>.json`. The key is
  the repo root path with every character outside `[A-Za-z0-9]` replaced by
  `-`, the rule `~/.claude/projects` uses (`/home/wai/.claude/mates` →
  `-home-wai--claude-mates`). Writes are atomic and taken under `flock` on
  `<repo-key>.lock`.
- **Repo root** is `git rev-parse --show-toplevel` of where `mates` ran. The
  lead always `cd`s there before starting `claude`, so its transcript is under
  the root's key.
- **Pane identity:** every mates pane carries the tmux pane option
  `@mates=<repo-key>:<n>` (`<repo-key>:lead` for Claude), set at split time.
  Every command finds a pane by listing `#{pane_id} #{@mates}` across all
  sessions. A stored tmux pane id is never trusted: ids are reused after a
  tmux server restart. The mates window carries `@mates-window=<repo-key>`.
- **Qwen's code runs only in the sandbox until it is merged.** Nothing in a
  Qwen worktree is executed as Wai outside `bin/qwen`'s sandbox: no tests, no
  scripts, no build. The lead runs checks with `mates check`. After merge the
  code is in the lead's checkout and runs like any code the lead reviewed.
- **Git on a Qwen worktree** goes through one helper, `git_safe`:
  - It never lets git find the repo from Qwen's folder. It sets `GIT_DIR`,
    `GIT_COMMON_DIR` and `GIT_WORK_TREE` from the state file and the lead's
    repo, and uses its own temporary index. (Tested 2026-11-10: a worktree
    whose `commondir` was pointed at a made-up folder ran a command from that
    folder's config on `git add`; with the three variables set it did not.)
  - It adds `-c core.hooksPath=/dev/null -c core.fsmonitor=false` and, for
    diffs, `--no-ext-diff --no-textconv`, so a tracked hooks folder (husky,
    `.githooks/`) that Qwen edited cannot run as Wai.
  - The lead-checkout fast-forward in `merge` uses the same flags.
- **Qwen has no write access to git at all** in a mates pane: the shared git
  folder and the worktree's `.git` file are read-only (Task 1). `git status`
  and `git diff` still work there.
- **Panes:** Claude takes the left half. Each Qwen pane opens with
  `tmux split-window -h` (side by side; Wai: "make sure claude opens them with
  vertical split"). Nothing re-tiles, and Wai resizes panes by hand. This
  replaces the grid and its 25-column minimum.
- **Worktrees:** `<repo>/.claude/worktrees/mates-qwen-<n>`, on branch
  `mates/qwen-<n>`, made from the lead's committed HEAD. `.mates/` and
  `/.claude/worktrees/` are added to the file
  `git rev-parse --git-path info/exclude` names (right for a main checkout and
  for a root that is itself a worktree).
- **One open task at a time** across all Qwen panes, until Task 0's
  two-pane measurement says otherwise; then `"parallel": true` in the state
  file allows more. Wai can talk to any pane at any time; this limits only
  Claude's `send`.
- **Messages:** Claude's messages to Qwen start with `[from Claude]`. The
  protocol lives in `bin/qwen-sandbox.md`.
- **Two failed tries on a task:** Claude does it itself, and logs it
  `escalated`.
- **Audit log:** `~/.claude/mates/log.jsonl` via `logrec`, one line per
  finished attempt (Task 6 lists the fields). Routes: `qwen` / `claude-self` /
  `claude-subagent`. Outcomes: `accepted`, `sent-back`, `escalated`, `failed`.
- Never `--dangerously-skip-permissions`.

## Review Focus

1. **Qwen-written code reaching Wai's rights before merge.** Expected: every
   check runs through `mates check` (sandboxed); every git step on a Qwen
   worktree uses `git_safe`, which never trusts Qwen's git folder. Tests in
   Task 5 (`test_check_runs_in_sandbox`, `test_merge_ignores_tracked_hooks`,
   `test_git_safe_ignores_redirected_commondir`) and Task 1 (`sandbox-test`
   lines for git refs, objects and the `.git` file).
2. **Qwen's worktree is dirty when Claude sends a new task.** Expected: `send`
   refuses and says so, and points at `mates merge N` or `mates discard N`; it
   never resets anyone's work. Test in Task 5
   (`test_send_refuses_dirty_worktree`).
3. **What merges is what was checked.** Expected: `merge` commits the exact
   snapshot `check` ran on, whatever Qwen or the check did to the folder
   since. If the lead branch moved, `merge` refuses and `sync` carries the
   work over, clearing the check; on a conflict `sync` changes nothing. Tests
   in Task 5 (`test_merge_commits_checked_snapshot`,
   `test_merge_refuses_when_lead_moved`, `test_sync_conflict_changes_nothing`).
4. **Restore after the whole window is gone, a saved session is gone, or
   `qwen` fails to start.** Expected: every Qwen entry not marked closed comes
   back; a missing OpenCode or Claude session starts that pane fresh with a
   warning; a pane whose `qwen` exits non-zero keeps a shell with the error
   and is not marked closed. Tests in Task 3
   (`test_restore_after_all_panes_gone`) and Task 4
   (`test_restore_missing_session_fresh`, `test_failed_qwen_pane_not_closed`).
5. **`mates` run twice, in a window with panes, or outside tmux.** Expected:
   a live mates window for this repo is switched to; otherwise a new window
   or session, never splitting someone's layout. Test in Task 4
   (`test_launch_window_choice`).

---

## File map

| File | Responsibility |
|---|---|
| `bin/qwen`, `bin/qwen-sandbox.md` | `exec` mode, read-only git for mates, runtime mount; protocol text |
| `mates/layout.py` | `split_qwen`: the tmux argv for the next side-by-side Qwen pane |
| `mates/session.py` | state load/save under flock, pane lookup by tag, OpenCode and Claude session lookup, worktree ensure |
| `mates/cli.py` | `launch` internals, `send`, `wait`, `answer`, `check`, `diff`, `sync`, `merge`, `discard`, `add`, `remove`, `list`, `log`, `log-route`, `retro` |
| `mates/guard.py` (modify) | add a hook-path flag |
| `mates/logrec.py` (modify) | add `questions`, `pane`, `tree` fields |
| `bin/mates` | entry point: window choice, then `cli.py launch`, or a subcommand |
| `mates/lead.md`, `mates/lead-lessons.md` | lead role prompt and lessons |
| `mates/prompts/retro.md`, `deploy/mates-retro.{service,timer}` | daily retro |
| `~/src/hosaka` (other repo) | refuse to start while a `qwen` lock is live |

---

### Task 0: Probe by hand, before any build (about 20 minutes, Wai watches)

Every later task leans on these. Run them in a scratch repo with plain
`qwen` in a second tmux pane, and write the answers to `docs/mates-probe.md`.

- [ ] **Paste:** `tmux load-buffer` a 3-line message, then
  `paste-buffer -p` (bracketed) into the OpenCode pane, then `send-keys Enter`.
  Does it arrive as **one** message? If not, try `paste-buffer` without `-p`
  and a single line with the newlines replaced by ` / `. Record which works.
  If none does, stop and report.
- [ ] **Empty input box:** `tmux capture-pane -p` of the pane at idle, with
  text half-typed, and mid-reply. Record what tells the three apart.
- [ ] **Busy signal:** in `opencode.db`, does an assistant `message.data`
  carry `time.completed` once the reply ends, and not before?
- [ ] **Protocol:** send a `[from Claude]` toy task. Does Qwen write
  `.mates/done.md` and stop? Send an ambiguous one. Does it write
  `.mates/question.md`?
- [ ] **New session:** paste `/new` and Enter into the pane. Does OpenCode
  start a new session, and does `opencode.db` show it as the newest for that
  folder?
- [ ] **Restore:** quit OpenCode, then `qwen -s <id>`. Same conversation?
- [ ] **Claude title:** start `claude -n "mates probe"`, then `/clear`. Does the
  new transcript carry the custom title? (If not, `lead.md` tells the lead to
  `/rename "mates <repo>"` after a clear.)
- [ ] **Two panes at once:** two OpenCode panes, same 5-minute task in each,
  started together. Time both against one alone, once with the server's
  `OLLAMA_NUM_PARALLEL=1` (as `bin/qwen` sets it; the panes take turns) and
  once with `2`, recording VRAM on both GPUs and whether anything lands on
  CPU. `"parallel"` may default to true only if both finish within 1.3× the
  solo time with nothing on CPU.
- [ ] **Commit** `docs/mates-probe.md`: "docs(mates): probe of OpenCode paste, protocol, restore".

### Task 1: `bin/qwen` additions

**Files:**
- Modify: `bin/qwen`, `bin/qwen-sandbox.md`, `docs/qwen.md`

**Interfaces:**
- `qwen exec -- <cmd...>` runs `<cmd>` through `run_sandboxed` in the current
  directory (same mounts, no network), returns its exit code, and starts no
  TUI. It starts **no model server and no relay**: a check does not load the
  GPU, stop hosaka, or get a line to the model. `mates check` uses it.
- `QWEN_GIT_RO=1`: the whole shared git folder is bound **read-only**, and so
  is the worktree's `.git` file. Qwen can run `git status` and `git diff`
  but cannot move a branch, write an object, stage a file, or repoint the
  worktree at another git folder. mates panes and `mates check` always set
  it. (Tested 2026-11-10 inside bwrap: status and diff work; writing a ref,
  editing or renaming `.git`, and `git add` all fail.)
- **OpenCode state per repo:** the sandbox gets
  `$STATE/opencode/<key>/` instead of the one shared folder, where `<key>`
  is the repo key of the main checkout (the parent of
  `git rev-parse --git-common-dir`), so a repo's worktrees share it and no
  other repo's sessions are visible. `mates` reads the same folder.
- **GPU lock:** each TUI run (not `exec`) writes `$STATE/locks/<pid>` and
  removes it on exit. Task 7's hosaka change reads these, so plain `qwen`
  and mates panes are both covered.
- `qwen warm` starts (or reuses) the server and loads the model in the
  background.
- `QWEN_RUNTIME=<dir>`: bind `<dir>` read-only at the same path and add the
  env lines `mates.runtime.runtime_env(<dir>)` gives (PATH first).
- `bin/qwen-sandbox.md`: `done.md` is two lines: what changed (files), and
  the test status as one of `passed`, `failed: <why>`, `could not run: <why>`.
  "Never install anything; if a tool is missing, write `could not run`."
- `sandbox-test` gains lines: with `QWEN_GIT_RO=1` in a worktree, writing a
  branch ref, writing an object and editing the `.git` file are `blocked`;
  another repo's OpenCode folder is not there.

- [ ] **Step 1: Failing tests** (`tests/test_qwen_bin.sh`, skipped without
  bwrap): `exec` returns the command's exit code; `exec` cannot reach
  `/home/wai`; `exec` starts no `ollama` process; with `QWEN_GIT_RO=1`,
  `git update-ref refs/heads/x HEAD`, `git add` and `mv .git x` fail and
  `git status` works; `QWEN_RUNTIME` puts its `bin` first on PATH; a lock
  file exists while a TUI run is up and is gone after.
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** "feat(qwen): exec mode, read-only git, per-repo OpenCode state, GPU lock, runtime mount".

### Task 2: Pane splits

Wai, 2026-11-10: "I know how to resize panes with a shortcut, it's OK, just
make sure claude opens them with vertical split", meaning vim-style side by
side, which is `tmux split-window -h`. There is no grid, no re-tiling and no
`mates layout`. Wai resizes panes by hand.

**Files:**
- Create: `mates/layout.py`
- Test: `tests/test_layout.py`

**Interfaces:**
- `split_qwen(window_panes: list[str], lead_pane: str) -> list[str]` returns
  the tmux argv for the next Qwen pane:
  - the first Qwen pane: `split-window -h -t <lead> -l 50%`, so Claude keeps
    the left half;
  - later panes: `split-window -h -t <rightmost Qwen pane>`, side by side.
  - It never moves or resizes existing panes while you work.
- **Widths survive restore (as spire-trio does):** `save()` records each pane's
  width as a percent of the window (`#{pane_width}` / `#{window_width}`). After
  restore has made all the splits, `restore_widths(panes, pcts)` resizes each
  pane except the last (`resize-pane -x <pct>%`), which takes the rest. That
  puts back what Wai set by hand; it is not re-tiling.
- **A split that doesn't fit** (`split-window` fails: "no space for new pane")
  makes `add` print "no room for another Qwen pane: widen the window or close
  a pane". It changes nothing.

- [ ] **Step 1: Failing tests:**
  - `test_first_qwen_splits_lead_half`: argv contains `-h`, `-t %lead`, `-l 50%`.
  - `test_next_qwen_splits_rightmost`: with Qwen panes `%2,%3`, the argv has
    `-h -t %3` and no `-l`.
  - `test_restore_widths_reapplied` (stub tmux records `resize-pane -x 50%`,
    `-x 20%` for 3 panes saved at 50/20/30, and nothing for the last).
  - `test_add_no_room_message` (stub tmux exits 1 with "no space for new pane").
  - `test_real_tmux_side_by_side` (skipped without tmux): in a detached
    `new-session -x 240 -y 60`, one lead plus 2 Qwen panes all have the same
    top (side by side), and the lead is about 120 wide.
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** "feat(mates): qwen panes open side by side".

### Task 3: Session state and panes

**Files:**
- Create: `mates/session.py`
- Test: `tests/test_session.py`

**Interfaces:**
- `repo_key(root) -> str` (rule in Global Constraints).
- `load(root) -> dict|None` and `save(root, state)`: atomic, under flock.
  `save` **never drops entries.** The state is:
  `{"root", "claude_session": uuid, "claude_title": "mates <repo>", "parallel": false, "qwens": [{"n", "worktree", "branch", "opencode_session"|None, "closed": false, "task": {...}|None}]}`.
  `task` is `{id, summary, reason, files, base, sent_ts, attempt_ts, attempts, questions: [{q, a}], checked_tree|None, check_cmd|None}`.
- `find_pane(root, n) -> str|None` and `find_lead(root) -> str|None`: look up
  by the `@mates` tag. `tag_pane(pane_id, root, n)`.
- `mark_closed(state, n)`: called when a live lead's command finds pane N
  gone. A pane that still exists but lost its tag (its `qwen` failed to
  start; Task 4's wrapper) is reported as `failed`, never marked closed.
  Closed entries are skipped on restore; `add` reuses the lowest closed `n`
  and its worktree.
- `ensure_worktree(root, n) -> (path, branch)` creates or reuses
  `.claude/worktrees/mates-qwen-<n>` on `mates/qwen-<n>` from HEAD, and adds
  both exclude lines.
- `opencode_session(worktree, db=~/.local/state/qwen/opencode/<key>/opencode.db) -> str|None`
  returns the newest `session.id` with `directory = worktree` and no
  `parent_id`, from a read-only connection.
- `opencode_busy(session, db) -> bool`: the newest assistant message lacks
  `time.completed` (as confirmed in Task 0) **and** some `part` of the
  session was updated in the last 300 s. A crashed OpenCode therefore stops
  counting as busy.
- `claude_session(root, title) -> str|None`: the newest transcript under
  `~/.claude/projects/<repo-key>/` whose last `custom-title` is `title`
  (spire-trio's `session_info` logic). Used at every save, so `/clear` or a
  resumed fork is followed.

- [ ] **Step 1: Failing tests:**
  - `test_roundtrip`
  - `test_repo_key_matches_claude_projects` (`/home/wai/.claude/mates` →
    `-home-wai--claude-mates`)
  - `test_restore_after_all_panes_gone` (no live panes, save, load: every
    entry is still there and not closed)
  - `test_find_pane_by_tag` (stub tmux listing; a reused pane id with another
    tag is not returned)
  - `test_ensure_worktree_idempotent` (temp repo; also with the root itself a
    worktree, where `.git` is a file)
  - `test_opencode_session_newest_for_dir` (fixture sqlite DB with the
    `session` columns `id, directory, parent_id, time_updated`)
  - `test_claude_session_follows_title` (two transcripts, newest titled wins)
  - `test_busy_expires_without_updates`
  - `test_untagged_pane_is_failed_not_closed`
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** "feat(mates): per-repo session state, panes found by tag".

### Task 4: Launcher

**Files:**
- Create: `bin/mates`, `mates/cli.py` (`launch`, `launch_plan`)
- Create: `mates/lead.md`, `mates/lead-lessons.md`
- Test: `tests/test_launcher.sh`, `tests/test_cli.py`

**Interfaces:**
- `bin/mates [--fresh] [-- <claude args>]`, run in a git repo:
  - **Already running:** if a window tagged `@mates-window=<repo-key>`
    exists and its lead pane is running `claude`, switch to it
    (`switch-client` / `attach`) and exit. If the window exists but the lead
    has exited, restart the lead in that pane and leave the Qwen panes alone.
  - **Window choice:** outside tmux,
    `tmux new-session -s mates-<repo-basename>-<4 hex of key>` running
    `bin/mates`. Inside tmux, use the current window if it has one pane, else
    `tmux new-window` running `bin/mates`.
- `cli.py launch [--fresh]` builds a plan from the state:
  - **Claude:** fresh means `--session-id <new uuid> -n "mates <repo>"`;
    restore means `--resume <id from claude_session(), else the saved uuid>`;
    if that transcript is gone, fresh with a warning.
  - **Qwen panes:** one entry `n=1` when fresh, otherwise every entry not
    closed, each `qwen -s <id>`, or plain `qwen` with a warning if its
    OpenCode session is gone.
  - **Steps:**
    1. Warm start: `qwen warm`, so the model loads while panes start.
    2. (The GPU lock is `bin/qwen`'s; see Task 1.)
    3. If the repo has a tracked `.mates/runtime.sh`, `ensure_runtime` at
       HEAD; its dir becomes `QWEN_RUNTIME` for every pane and every check.
    4. Split the Qwen panes, tag each, and in each run a wrapper:
       `cd <worktree> && QWEN_GIT_RO=1 QWEN_RUNTIME=... qwen ...`. When
       `qwen` exits 0 the pane closes. On any other exit the wrapper clears
       the pane's `@mates` tag, prints the error and leaves a shell, so the
       failure is seen and a later `send` cannot paste into a shell.
    5. Tag the lead pane and the window, save the state.
    6. Run the lead in this pane, from the repo root:
       `env -u ANTHROPIC_API_KEY MATES_LEAD=1 MATES_ROOT=<root> command claude <session args> --append-system-prompt "$(cat lead.md lead-lessons.md [~/.claude/private.md])" <passed args>`.
       `private.md` is included when readable, as Wai's `claude` wrapper does.
       `bin/mates` stays the parent (no `exec`): if `claude` exits
       non-zero it leaves a shell with the message (as spire-trio does),
       never a closed pane.
- Subcommands refuse unless `MATES_LEAD=1`, except `list`, `log` and `retro`.
- **`lead.md`:**
  - the routing rule (quoted); "no check, no Qwen";
  - **never run code from a Qwen worktree yourself: use `mates check`**;
  - the loop:
    `mates send N --reason "<why Qwen>" --files a,b "<task + files + how to check>"`,
    then a Monitor on `mates wait N`, then `mates diff N` and
    `mates check N -- <cmd>`, then `mates merge N` or
    `mates answer N --sendback "<notes>"`; if `merge` says the lead moved,
    `mates sync N` and check again;
  - Wai's own work with Qwen merges on request:
    `mates check N -- <cmd>`, then `mates merge N -m "<summary>"`;
  - two failures, then `mates discard N` and Claude does it;
  - `mates log-route claude-self "<summary>" "<reason>"` for Claude-only work;
  - Qwen's own "passed" is a claim; only `mates check` counts;
  - one open task at a time unless the state says `parallel`.

- [ ] **Step 1: Failing tests:**
  - `test_launch_plan_fresh` (1 Qwen, new uuid)
  - `test_launch_plan_restore` (N Qwen, closed entries skipped, resume ids)
  - `test_restore_missing_session_fresh` (missing Claude transcript and
    missing OpenCode session: both fresh, both warned, the rest restored)
  - `test_subcommand_refused_without_lead_env`
  - `tests/test_launcher.sh::test_launch_window_choice`: a detached tmux with
    stub `claude`/`qwen` (`MATES_CLAUDE`, `MATES_QWEN` env overrides) checks
    that a 1-pane window is reused, a 2-pane window gets a new window, a
    second `mates` in the same repo switches to the first, a window whose
    lead exited gets its lead restarted, and a failing stub `claude` leaves
    a shell.
  - `test_failed_qwen_pane_not_closed` (stub `qwen` exits 1: the pane stays,
    untagged, and the entry is not closed)
  - `test_fresh_keeps_worktrees` (a dirty worktree is warned about, not reset)
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** "feat(mates): launcher with fresh/restore". Wai links `~/bin/mates`.

### Task 5: Delegation: send, wait, answer, check, diff, sync, merge, discard

**Files:**
- Modify: `mates/cli.py`, `mates/guard.py`
- Test: `tests/test_cli.py`, `tests/test_guard.py`

**Interfaces:**
- `mates send N --reason "<why Qwen>" [--files a,b] [--continue] "<text>"`:
  1. Refuse if pane N is missing (and mark it closed), if the pane is not
     running the sandbox (`#{pane_current_command}`), if OpenCode is busy,
     if the pane's input box is not empty (Task 0's capture rule), if another
     pane has an open task and `parallel` is false, or if the worktree has
     uncommitted or unmerged changes (message names `merge` and `discard`).
  2. Rebase `mates/qwen-N` onto the lead HEAD (`git_safe`).
  3. Delete `.mates/done.md` and `.mates/question.md`.
  4. Record the task in the state (id, summary, reason, files, base, ts,
     attempts).
  5. Start a new OpenCode session (paste `/new`, as Task 0 confirmed), so
     the task gets a clean context. `--continue` skips this, for a task that
     builds on what Wai and Qwen just discussed.
  6. Paste `[from Claude] <text>` the way Task 0 found works
     (`load-buffer` + `paste-buffer -p -d`, then `send-keys Enter`).
  7. If attempts ≥ 2, print a reminder to discard and escalate.
- `mates wait N [--timeout S]` (default 1800 s) prints one line and exits:
  `question: <text>`, `done: <text>`, `dead` (pane gone; marked closed),
  `failed` (the pane lost its tag: `qwen` exited with an error),
  `idle` (OpenCode not busy, no `part` updated for 300 s, neither file), or
  `timeout`. Qwen's text is cut at 2,000 characters. It's built for a Monitor
  until-loop.
- `mates answer N "<text>"` deletes **both** files, pastes
  `[from Claude] <text>` into the same OpenCode session, and records the Q&A
  in the task. With `--sendback` it counts an attempt, logs `sent-back`,
  and clears `checked_tree`.
- `mates check N -- <cmd>`:
  1. Snapshot the worktree as a git tree (`git_safe`, a temp index,
     `add -A ':!.mates'`, `write-tree`).
  2. Unpack that tree into `<worktree>/.mates/check/<tree>/`
     (`git archive | tar -x`; an older check folder is removed first).
  3. Run `<cmd>` there with `qwen exec` (`QWEN_GIT_RO=1`, the runtime).
  4. Print the exit code and the last 20 lines. On exit 0, record the tree
     as `checked_tree` and the command as `check_cmd`.
  The check runs on the snapshot, so nothing Qwen does to the folder
  afterwards, and nothing the check leaves behind, can reach the merge.
  With no open task, the base is the merge-base with the lead branch.
- `mates diff N` prints, with `git_safe`: the diff against the task base
  including untracked files (`add -N` on a temp index), a diff stat, and
  `guard_flags` with the task's `--files`. `guard.py` gains a flag for any
  path under `.githooks/`, `.husky/`, the repo's `core.hooksPath`, or any
  `.git*` file, and a flag for any file the runtime hash covers plus the
  common manifests and lockfiles (`pyproject.toml`, `uv.lock`,
  `requirements*.txt`, `package.json`, `package-lock.json`, `Cargo.toml`,
  `Cargo.lock`), because the next launch rebuilds the runtime from them as
  Wai, with network. Any flag means the lead reads the full diff.
- `mates merge N [-m "<summary>"]`:
  1. Refuse unless there is a `checked_tree`, the lead checkout is on a
     branch (not detached), has no uncommitted changes in paths the diff
     touches, and **the lead HEAD is still the task base**. If the lead
     moved, print `lead moved: run mates sync N, then check again`.
  2. Make the commit from the snapshot, not from the folder:
     `commit-tree <checked_tree> -p <base> -m "mates qwen-N: <summary>"`
     (the summary is the task's, or `-m` when Wai worked with Qwen directly).
     "What merges is what was checked" holds by construction.
  3. Fast-forward the lead checkout to that commit (`git_safe`,
     `merge --ff-only`).
  4. Point `mates/qwen-N` at the new HEAD and reset the worktree to it;
     if the folder differs from the snapshot (Qwen kept editing), snapshot
     the difference to `refs/mates/discarded/<n>-<ts>` first and say so.
  5. Clear the task. Log `accepted` with the cost fields (Task 6).
  Qwen cannot commit (git is read-only for it), so there are no Qwen commits
  to fold.
- `mates sync N`, for when the lead branch moved after the task was sent:
  1. Snapshot the worktree as a commit on the old base (`commit-tree`).
  2. Cherry-pick it onto the lead HEAD in a scratch index
     (`merge-tree --write-tree`), touching no folder.
  3. On a conflict: print `conflict` and the paths, and change nothing. The
     lead resolves by sending the task back with notes, or discards.
  4. Otherwise check the merged tree out into the worktree, set the task
     base to the lead HEAD, and clear `checked_tree`, so the check must run
     again on what will merge.
- `mates discard N`: snapshot the worktree (tracked and untracked) as a
  commit on `refs/mates/discarded/<n>-<ts>`, reset the worktree to the lead
  HEAD, clear the task, and log `escalated` (or `failed` with `--failed`).
  Nothing is deleted; the snapshot ref stays.

- [ ] **Step 1: Failing tests** (stub tmux records buffers and `send-keys`; stub `qwen exec`; temp repos):
  - `test_send_pastes_prefixed_message` (one paste, then one Enter)
  - `test_send_refuses_dirty_worktree`, `test_send_refuses_busy`,
    `test_send_refuses_second_open_task`, `test_send_refuses_shell_pane`
  - `test_send_rebases_clean_worktree`
  - `test_send_starts_new_session` (and `--continue` does not)
  - `test_wait_done`, `test_wait_question`, `test_wait_dead_pane`,
    `test_wait_idle`, `test_wait_timeout`
  - `test_answer_clears_stale_done`
  - `test_check_runs_in_sandbox` (the command goes through `qwen exec`,
    never `subprocess` in the worktree)
  - `test_check_runs_on_snapshot` (a file the check writes is not in the
    worktree's tracked or untracked set outside `.mates/`)
  - `test_merge_fast_forwards_and_resets`
  - `test_merge_refuses_without_check`
  - `test_merge_commits_checked_snapshot` (the folder is edited after the
    passing check: the merged commit has the checked tree, and the later
    edit is kept on a `refs/mates/discarded/` ref)
  - `test_merge_refuses_when_lead_moved`
  - `test_sync_carries_work_and_clears_check`,
    `test_sync_conflict_changes_nothing`
  - `test_merge_refuses_dirty_lead_paths`, `test_merge_refuses_detached_lead`
  - `test_merge_without_task_uses_merge_base`
  - `test_merge_excludes_mates_dir`
  - `test_git_safe_ignores_redirected_commondir` (the worktree's `commondir`
    points at a folder whose config has a `filter` that writes a canary: no
    canary)
  - `test_guard_flags_manifests`
  - `test_merge_ignores_tracked_hooks` (repo with `core.hooksPath=.githooks`
    and a Qwen-edited `pre-commit` that writes a canary: no canary)
  - `test_discard_keeps_snapshot_ref`
  - `test_guard_flags_hook_paths`
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** "feat(mates): send, wait, answer, check, diff, sync, merge, discard".

### Task 6: Panes, housekeeping and the log

**Files:**
- Modify: `mates/cli.py`, `mates/logrec.py`
- Test: `tests/test_cli.py`, `tests/test_logrec.py`

**Interfaces:**
- `mates add` reuses the lowest closed entry or creates the next worktree,
  splits (side by side, `split_qwen`) and tags a Qwen pane, and saves.
- `mates remove N` refuses if worktree N is dirty (unless `--force`, which
  snapshots like `discard` first). It kills the pane, removes the worktree
  (`git worktree remove`; plain `shutil.rmtree` for leftovers, since Qwen
  panes run as Wai) and the branch, and drops the entry. This is
  the only command that drops an entry.
- `mates list` prints a table: n, pane, branch, dirty?, open task, attempts,
  OpenCode session, closed?
- `mates log [--repo] [--route] [--outcome]` prints a table.
- `mates log-route <route> "<summary>" "<reason>" [--outcome accepted]`
  appends a log line with a new task id.
- **Log line per finished attempt** (`accepted`, `sent-back`, `escalated`,
  `failed`), with these filled in:
  - `repo`, `task_id`, `summary`, `route` = `qwen`, `reason` (from `send
    --reason`), `attempts`, `outcome`;
  - `check` and `check_sha` from the last `mates check`, `tree`;
  - `flags` from `mates diff`, and whether the lead ran it;
  - `questions`: the Q&A list (new field);
  - `worker_wall_s`: this attempt's start to its finish; `worker_tokens`:
    the sum of `tokens.input + tokens.output` of the OpenCode session's
    assistant messages created since this attempt started (so send-backs
    are not counted twice);
  - `answered_by`: the model the server reports loaded (`/api/ps`) at the
    finish, not the name the client asked for;
  - `lead_tokens`: the usage of the lead transcript's assistant turns that
    ran a `mates` command naming this pane, during this attempt (the spec's
    rule; rough, within a factor of 2). Other work the lead did while
    waiting is not counted.
  - `logrec.FIELDS` gains `questions`, `pane`, `tree`.

- [ ] **Step 1: Failing tests:**
  - `test_add_then_restore_has_two`
  - `test_add_reuses_closed`
  - `test_remove_refuses_dirty`, `test_remove_is_only_drop`
  - `test_log_filters`
  - `test_log_line_has_cost_fields` (fixture OpenCode DB and transcript)
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5: Real end-to-end check** (Wai watches):
  1. `mates` in a scratch repo.
  2. The lead sends the probe task to Qwen and waits.
  3. Qwen writes `done.md`; the lead diffs, runs `mates check`, and merges.
  4. A second task gets a deliberately wrong check; the lead sends back
     twice, then discards and does it itself (`escalated` in the log).
  5. `mates` is closed and reopened: the same Claude conversation and Qwen
     session come back. A second `mates` in the repo switches to the first.
- [ ] **Step 6: Commit** "feat(mates): add/remove panes, list, log with cost fields".

### Task 7: Hosaka lock (repo `~/src/hosaka`)

**Interfaces:**
- hosaka's server start refuses, with "qwen is using the GPU (<n> session(s));
  close them or run `qwen stop`", while any
  `~/.local/state/qwen/locks/<pid>` names a live pid. Dead pids are stale:
  removed, and start goes on.
- The locks are written and removed by `bin/qwen` (Task 1), one per running
  Qwen pane, whether `mates` or plain `qwen` started it. Closing the lead
  while Qwen panes stay up keeps the GPU locked, which is right.

- [ ] **Step 1: Failing test** in hosaka's suite: a live-pid lock refuses; a
  dead-pid lock is cleared.
- [ ] **Step 2–4:** fail, implement, pass.
- [ ] **Step 5: Commit** in hosaka: "feat: refuse to start while qwen holds the GPU".

### Task 8: Daily retro

**Files:**
- Create: `mates/prompts/retro.md`, `deploy/mates-retro.service`, `deploy/mates-retro.timer`
- Modify: `mates/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- `mates retro [--date D]`:
  1. Skip a day with no log lines.
  2. Build the input: that day's log lines with **the question text
     removed** (the count stays), plus the last 7 retros. The question text
     is the only part of a log line Qwen wrote; `summary`, `reason` and the
     answers are the lead's own words, and the retro needs them to find
     misroutes and weak briefs.
  3. Run `claude -p --tools "" --strict-mcp-config --setting-sources project --output-format json`
     from `/srv/mates/neutral`. No tools at all. The prompt asks for JSON:
     `{"findings_md", "lessons": [str], "proposals": [str]}`, where
     `lessons` is the **whole new list** (it replaces the file, so the retro
     can drop or merge old lessons).
  4. `cli` writes `~/.claude/mates/retros/D.md` from `findings_md`.
  5. `cli` checks each lesson: at most 200 characters; no repo name or file
     name that appears in any log line; nothing that looks like a path (a
     leading `~/` or `/`, or `word/word` ending in a file extension; plain
     "pass/fail" is fine). It keeps the first 20 that pass, writes them to
     `mates/lead-lessons.md`, and reports the rejected ones in the retro file.
  6. Commit only when the mates source checkout (the one `~/bin/mates`
     resolves to) is on `main` and `lead-lessons.md` is its only change:
     `git commit -m "mates: lessons from retro D" -- mates/lead-lessons.md`.
     Otherwise the file is written, not committed, and the retro file says
     why.
  7. `proposals` (routing rule, concurrency, model) are appended to that
     checkout's gitignored `OPEN-QUESTIONS.md` under `## Open`, never
     applied.
- The timer runs daily at 23:30 with `Persistent=true` (user lingering is on).

- [ ] **Step 1: Failing tests** (stub claude):
  - `test_retro_skips_empty_day`
  - `test_retro_input_has_no_question_text` (summary and reason are kept)
  - `test_retro_rejects_lesson_with_repo_name`,
    `test_retro_allows_slash_words`
  - `test_retro_lessons_replace_file`
  - `test_retro_no_commit_when_tree_dirty`
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5:** Wai enables the timer (`systemctl --user enable --now mates-retro.timer` after linking).
- [ ] **Step 6: Commit** "feat(mates): daily retro".
