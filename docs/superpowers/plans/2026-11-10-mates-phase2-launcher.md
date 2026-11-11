# Mates Phase 2 (two-pane `mates`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `mates` in a git repo opens Claude Code on the left and one or more
sandboxed `qwen` (OpenCode) panes on the right. Each Qwen pane works in its
own worktree. Claude delegates by typing into a Qwen pane as Wai, waits on
`.mates/done.md` or `.mates/question.md`, reviews, and merges. It restores the
previous Claude conversation and all Qwen panes the way spire-trio does.

**Architecture:**
- **`bin/mates`** (bash) launches and restores the tmux window, and
  dispatches subcommands to `mates/cli.py`.
- **`mates/cli.py`** works on a per-repo session file and the panes. There is
  no daemon.
- **Qwen panes run `bin/qwen`,** unchanged except for passing args through to
  OpenCode, which supplies the sandbox.

**Tech Stack:** bash, Python 3.12 stdlib (sqlite3 to read OpenCode's session DB), tmux 3.4, Claude Code (`--session-id`, `--resume`, `-n`), OpenCode 1.19.5 (`-s <session>`).

**Spec:** `docs/superpowers/specs/2026-11-10-claude-qwen-mates-design.md`. The
section "Revision 2026-11-10 (evening): two-pane mates" wins over everything
below it.

## Global Constraints

- The commands are exactly `mates` and `qwen`. Plain `claude` stays untouched.
- **Fresh vs restore:**
  - First run in a repo: 1 Claude and 1 Qwen.
  - Later runs: restore the Claude conversation and every Qwen pane that was
    open (Wai, verbatim).
  - `mates --fresh` starts over.
- **Session state:** `~/.claude/mates/sessions/<repo-key>.json`, where the key
  is the repo root path mangled the way `~/.claude/projects` mangles it. It's
  saved on every change.
- **Layout:**
  - Claude is pinned to 50% of the width, on the left.
  - The Qwen panes tile the right half in a grid of c columns by r rows,
    whose pane cols/rows is closest to 0.92, compared in log space.
- **Worktrees:** `<repo>/.claude/worktrees/mates-qwen-<n>`, on branch
  `mates/qwen-<n>`, made from the lead's committed HEAD. `.mates/` is added to
  the repo's `.git/info/exclude`.
- **Messages:** Claude's messages to Qwen start with `[from Claude]`. The
  protocol lives in `bin/qwen-sandbox.md` (already committed).
- **Two failed tries on a task:** Claude does it itself, and logs it
  `escalated`.
- **Audit log:** `~/.claude/mates/log.jsonl` via `logrec`. Routes:
  `qwen` / `claude-self`. Outcomes: `accepted`, `sent-back`, `escalated`,
  `failed`. Questions are logged with the task.
- Never `--dangerously-skip-permissions`.

## Review Focus

1. **Qwen's worktree is dirty when Claude sends a new task** (unmerged work,
   or work Wai did directly). Expected: `send` refuses and says so; it never
   resets someone's work. Test in Task 4 (`test_send_refuses_dirty_worktree`).
2. **The lead branch moved since Qwen's worktree was made.** Expected: on a
   clean tree, `send` rebases Qwen's worktree to the lead HEAD first. On a
   conflict, `merge` aborts the rebase and changes nothing. Test in Task 4
   (`test_merge_conflict_refuses`).
3. **The saved OpenCode session or Claude session no longer exists.**
   Expected: restore starts that pane fresh, with a warning, and the rest
   restores normally. Test in Task 2 (`test_restore_missing_session_fresh`).
4. **`mates` run in a window that already has panes, or outside tmux.**
   Expected: a new window or session, never splitting someone's layout. Test
   in Task 3 (`test_launch_window_choice`).
5. **The Qwen pane is closed by hand.** Expected: `wait` returns `dead`, and
   the next save drops that pane from the state. Test in Task 4
   (`test_wait_dead_pane`).

---

## File map

| File | Responsibility |
|---|---|
| `mates/layout.py` | `pick_grid`, tmux apply |
| `mates/session.py` | session-state load and save, OpenCode session lookup, worktree ensure |
| `mates/cli.py` | `send`, `wait`, `answer`, `diff`, `merge`, `add`, `remove`, `list`, `log`, `log-route`, `layout`, `retro`, plus `launch` internals |
| `bin/mates` | entry point: tmux window choice, then `cli.py launch`, or a subcommand |
| `mates/lead.md`, `mates/lead-lessons.md` | lead role prompt and lessons |
| `mates/prompts/retro.md`, `deploy/mates-retro.{service,timer}` | daily retro |

---

### Task 1: Layout

**Files:**
- Create: `mates/layout.py`
- Test: `tests/test_layout.py`

**Interfaces:**
- `pick_grid(cols: int, rows: int, n: int, target=0.92) -> tuple[int, int]`
  minimizes `abs(log((cols/c)/(rows/r)) - log(target))` over `c*r >= n`. Ties
  go to fewer empty cells.
- `apply(lead_pane: str, qwen_panes: list[str]) -> None`:
  - lead pane at 50% of the window width;
  - Qwen panes rebuilt into the grid on the right, using `tmux join-pane` /
    `split-window` plus `resize-pane`;
  - idempotent.

- [ ] **Step 1: Failing tests:**
  - `pick_grid(120,60,1)==(1,1)`
  - `pick_grid(120,60,2)==(2,1)`
  - `pick_grid(120,60,4)==(2,2)`
  - `pick_grid(60,80,2)==(1,2)`
  - `test_apply_real_tmux` (skipped without tmux): in a detached
    `new-session -x 240 -y 60` with 1 lead and 2 Qwen panes, expect the lead
    width 120±1 and the Qwen panes side by side.
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** "feat(mates): phone-shaped grid for qwen panes".

### Task 2: Session state

**Files:**
- Create: `mates/session.py`
- Test: `tests/test_session.py`

**Interfaces:**
- `repo_key(root) -> str`.
- `load(root) -> dict|None` and `save(root, state)`, with an atomic write. The
  state is `{"root", "claude_session": uuid, "qwens": [{"n", "worktree", "branch", "opencode_session"|None}]}`.
- `ensure_worktree(root, n) -> (path, branch)` creates or reuses
  `.claude/worktrees/mates-qwen-<n>` on `mates/qwen-<n>` from HEAD, and adds
  `.mates/` to `.git/info/exclude`.
- `opencode_session(worktree, db=~/.local/state/qwen/opencode/opencode.db) -> str|None`
  returns the newest `session.id` with `directory = worktree` and no
  `parent_id`. The DB is opened read-only.
- `claude_session_exists(root, uuid) -> bool` checks for
  `~/.claude/projects/<mangled root>/<uuid>.jsonl`.

- [ ] **Step 1: Failing tests:**
  - `test_roundtrip`
  - `test_ensure_worktree_idempotent` (temp repo)
  - `test_opencode_session_newest_for_dir` (fixture sqlite DB with the
    `session` columns `id, directory, parent_id, time_updated`)
  - `test_restore_missing_session_fresh` (with a missing uuid,
    `launch_plan()` marks Claude as fresh)
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** "feat(mates): per-repo session state".

### Task 3: Launcher

**Files:**
- Create: `bin/mates`
- Modify: `mates/cli.py` (`launch`)
- Create: `mates/lead.md`, `mates/lead-lessons.md`
- Test: `tests/test_launcher.sh`, `tests/test_cli.py`

**Interfaces:**
- `bin/mates [--fresh]`, run in a git repo:
  - **Window choice:** outside tmux, it runs
    `tmux new-session -s mates-<repo> "bin/mates"`. Inside tmux, it uses the
    current window if it has one pane, else opens `tmux new-window` running
    `bin/mates`.
- `cli.py launch [--fresh]` builds a plan from the state:
  - **Claude:** fresh means `--session-id <new uuid> -n "mates <repo>"`;
    restore means `--resume <uuid>`.
  - **Qwen panes:** one entry `n=1` when fresh, otherwise every saved entry,
    each `qwen -s <id>`, or plain `qwen` if its session is gone.
  - **Steps:**
    1. Save the state.
    2. Split the Qwen panes (each `cd <worktree> && exec qwen ...`).
    3. Apply the layout.
    4. `exec` the lead in this pane:
       `env -u ANTHROPIC_API_KEY MATES_LEAD=1 MATES_ROOT=<root> command claude <session args> --append-system-prompt "$(cat lead.md lead-lessons.md)"`.
- Subcommands (`bin/mates send ...` and the rest) refuse unless
  `MATES_LEAD=1`, except `list`, `log` and `retro`.
- **`lead.md`:**
  - the routing rule (quoted);
  - "no check, no Qwen";
  - the loop: `mates send N "<task + files + how to check>"`, then a Monitor
    on `mates wait N`, then `mates diff N` and run the check, then
    `mates merge N` or `mates answer N "<notes>"`;
  - two failures and Claude does it;
  - `mates log-route claude-self "<summary>" "<reason>"` for Claude-only work;
  - `mates add` when parallel work is truly independent.

- [ ] **Step 1: Failing tests:**
  - `test_launch_plan_fresh` (1 Qwen, new uuid)
  - `test_launch_plan_restore` (N Qwen, resume ids)
  - `test_subcommand_refused_without_lead_env`
  - `tests/test_launcher.sh::test_launch_window_choice`: a detached tmux with
    stub `claude`/`qwen` (`MATES_CLAUDE`, `MATES_QWEN` env overrides) checks
    that a 1-pane window is reused and a 2-pane window gets a new window.
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** "feat(mates): launcher with fresh/restore". Wai links `~/bin/mates`.

### Task 4: Delegation: send, wait, answer, diff, merge

**Files:**
- Modify: `mates/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- `mates send N "<text>"`:
  1. Refuse if pane N is missing, or its worktree has uncommitted or
     unmerged changes.
  2. Rebase `mates/qwen-N` onto the lead HEAD.
  3. Delete `.mates/done.md` and `.mates/question.md`.
  4. Record a task (id, summary, ts) in the session state.
  5. Type `[from Claude] <text>` into the pane: `tmux send-keys -l`, then
     `Enter`.
  6. Log route `qwen`.
- `mates wait N [--timeout S]` blocks until `.mates/question.md` (prints
  `question: <text>`), `.mates/done.md` (prints `done: <text>`), or the pane
  is gone (prints `dead`). It's built for a Monitor until-loop.
- `mates answer N "<text>"` deletes `question.md`, types
  `[from Claude] <text>`, and logs the Q&A.
- `mates diff N` prints `git -C <wt> diff` (tracked plus untracked, `.mates`
  excluded) and a diff stat.
- `mates merge N`:
  1. `git add -A ':!.mates'` and commit in the worktree
     (`-m "mates qwen-N: <task summary>"`).
  2. Rebase onto the lead branch.
  3. `git merge --ff-only mates/qwen-N` in the lead checkout.
  4. Reset the worktree to the new HEAD.
  5. Log `accepted`.
  - On a conflict: abort the rebase, print `conflict`, change nothing.
- A second `answer` with notes after a failed check counts as an attempt.
  After 2 attempts, `send` prints a reminder to escalate.

- [ ] **Step 1: Failing tests** (stub tmux records `send-keys`; temp repos):
  - `test_send_types_prefixed_message`
  - `test_send_refuses_dirty_worktree`
  - `test_send_rebases_clean_worktree`
  - `test_wait_done`, `test_wait_question`, `test_wait_dead_pane`
  - `test_merge_fast_forwards_and_resets`
  - `test_merge_conflict_refuses`
  - `test_merge_excludes_mates_dir`
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** "feat(mates): send, wait, answer, diff, merge".

### Task 5: Panes and housekeeping: add, remove, list, log, log-route, layout

**Files:**
- Modify: `mates/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- `mates add` creates the next worktree, splits a Qwen pane, applies the
  layout, and saves.
- `mates remove N` refuses if worktree N is dirty (unless `--force`). It
  kills the pane, removes the worktree and branch (`fsutil.rmtree` for the
  sandbox's files), relayouts, and saves.
- `mates list` prints a table: n, pane, branch, dirty?, last task, OpenCode
  session.
- `mates log [--repo] [--route] [--outcome]` prints a table.
- `mates log-route <route> "<summary>" "<reason>"` appends a log line.
- `mates layout` reapplies the grid.
- **State hygiene:** `save()` drops Qwen entries whose pane is gone before
  writing.

- [ ] **Step 1: Failing tests:**
  - `test_add_then_restore_has_two`
  - `test_remove_refuses_dirty`
  - `test_save_drops_dead_panes`
  - `test_log_filters`
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5: Real end-to-end check** (Wai watches):
  1. `mates` in a scratch repo.
  2. The lead sends the probe task to Qwen and waits.
  3. Qwen writes `done.md`; the lead diffs, runs the check, and merges.
  4. `mates` is closed and reopened: the same Claude conversation and Qwen
     session come back.
- [ ] **Step 6: Commit** "feat(mates): add/remove panes, list, log".

### Task 6: Daily retro

**Files:**
- Create: `mates/prompts/retro.md`, `deploy/mates-retro.service`, `deploy/mates-retro.timer`
- Modify: `mates/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- `mates retro [--date D]`:
  1. Skip a day with no log lines.
  2. Otherwise run
     `claude -p --setting-sources project --strict-mcp-config --tools Read,Edit`
     from `/srv/mates/neutral`, on that day's log lines plus the last 7
     retros.
  3. Write `~/.claude/mates/retros/D.md`.
  4. Allow edits only to `mates/lead-lessons.md` (task types only, no repo
     names); revert anything else.
  5. Commit the lessons file, dated.
  - Proposals to change the routing rule, concurrency or model go to
    `OPEN-QUESTIONS.md`; the retro never applies them.
- The timer runs daily at 23:30 with `Persistent=true`.

- [ ] **Step 1: Failing tests:**
  - `test_retro_skips_empty_day`
  - `test_retro_reverts_disallowed_edits` (stub claude)
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5:** Wai enables the timer (`systemctl --user enable --now mates-retro.timer` after linking).
- [ ] **Step 6: Commit** "feat(mates): daily retro".
