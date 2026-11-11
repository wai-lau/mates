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

**Pre-flight (same night):** the conflict scan's 22 conflicts and 11 defects
are resolved in place (rulings in
`.superpowers/sdd/2026-11-10-mates-phase2-launcher/rulings.md`). Task 0's
paste, protocol, question-channel and session-row probes are already
measured; Qwen does not ask when a task is ambiguous, so briefs leave it
nothing to guess.

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
  A Qwen pane whose `qwen` exited with an error keeps its tag and gets the
  pane option `@mates-failed=1` (Task 4's wrapper), so it is found as
  `failed`, never as gone.
- **Qwen's code runs only in the sandbox until it is merged.** Nothing in a
  Qwen worktree is executed as Wai outside `bin/qwen`'s sandbox: no tests, no
  scripts, no build. The lead runs checks with `mates check`. After merge the
  code is in the lead's checkout and runs like any code the lead reviewed.
- **Git on a Qwen worktree** goes through one helper, `git_safe`
  (`mates/gitsafe.py`, defined in Task 5):
  - It never lets git find the repo from Qwen's folder. It sets `GIT_DIR`,
    `GIT_COMMON_DIR` and `GIT_WORK_TREE` from the state file and the lead's
    repo, and uses its own temporary index. (Tested 2026-11-10: a worktree
    whose `commondir` was pointed at a made-up folder ran a command from that
    folder's config on `git add`; with the three variables set it did not.)
  - It adds `-c core.hooksPath=/dev/null -c core.fsmonitor=false` and, for
    diffs, `--no-ext-diff --no-textconv`, so a tracked hooks folder (husky,
    `.githooks/`) that Qwen edited cannot run as Wai.
  - The lead-checkout fast-forward in `merge` uses the same flags
    (`git_lead`, same file).
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
  file allows more. The default for new state files is the constant
  `PARALLEL_DEFAULT = False` in `session.py`; the builder sets it to `True`
  only if `docs/mates-probe.md` records the two-pane test as passed. Wai
  can talk to any pane at any time; this limits only Claude's `send`.
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
   Task 5 (`test_check_cannot_reach_home`, `test_merge_ignores_tracked_hooks`,
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
| `mates/gitsafe.py` | `git_safe` (git on a Qwen worktree, never trusting its folder) and `git_lead` (the lead checkout, same flags) |
| `mates/cli.py` | `launch` internals, `send`, `wait`, `answer`, `check`, `diff`, `sync`, `merge`, `discard`, `add`, `remove`, `list`, `log`, `log-route`, `retro`; shared helpers `paste`, `snapshot`, `worktree_dirty`, `log_attempt` |
| `mates/guard.py` (modify) | add hook-path and manifest flags |
| `mates/logrec.py` (modify) | add `questions`, `pane` fields |
| `bin/mates` | entry point: window choice, then `cli.py launch`, or a subcommand |
| `mates/lead.md`, `mates/lead-lessons.md` | lead role prompt and lessons |
| `mates/prompts/retro.md`, `deploy/mates-retro.{service,timer}` | daily retro |
| `~/src/hosaka` (other repo) | refuse to start while a `qwen` lock is live |

Untouched: Phase 1's `mates/sandbox.sh` (user-`claude` sandbox), `pilot.py`,
`tasks.py`, `fwd.py` and their tests stay as they are. `bin/qwen` keeps its
own bwrap mounts; nothing is shared with `sandbox.sh`.

Python modules import their siblings flat (`import session`), as Phase 1
does; `bin/mates` runs `python3 <mates dir>/cli.py`, which puts that dir on
`sys.path`. `bin/qwen` never imports Python from `mates/`.

**Test overrides** (used by every task's tests): `MATES_CLAUDE` (default
`claude`) and `MATES_QWEN` (default the `qwen` next to `bin/mates`) name the
programs `mates` runs; stub `tmux` goes first on `PATH`.

---

### Task 0: Probe by hand, before any build (about 20 minutes, Wai watches)

Every later task leans on these. Run them in a scratch repo with plain
`qwen` in a second tmux pane, and write the answers to `docs/mates-probe.md`.

- [x] **Paste** (measured 2026-11-10): `tmux load-buffer` a 3-line message,
  then `paste-buffer -p` (bracketed) into the OpenCode pane, then
  `send-keys Enter`, arrives as **one** message. This is the recipe; no
  fallback is built.
- [ ] **Input box captures:** `tmux capture-pane -p` of the pane at idle,
  with text half-typed, and mid-reply.
- [ ] **Busy signal and schema:** record `.schema` of the `session`,
  `message` and `part` tables. Does an assistant `message.data` carry
  `time.completed` once the reply ends, and not before? Does it carry
  `tokens.input` and `tokens.output`? Which column says when a `part` was
  last updated? Task 3's fixture DB and Task 6's token sum use exactly what
  is recorded here.
- [x] **Protocol** (measured 2026-11-10): a `[from Claude]` toy task made Qwen
  write `.mates/done.md` with a one-line summary and not commit. OpenCode's
  first request is about 8K tokens.
- [x] **Question channel** (measured 2026-11-10, `docs/mates-probe.md`): on
  an ambiguous `[from Claude]` task Qwen did **not** write
  `.mates/question.md`; it guessed and also edited a test to match. Nothing
  relies on Qwen asking: `lead.md` makes every brief name the exact symbols,
  files and check, and `mates diff` flags any test-file change
  (`guard_flags` already has `test-file`, `test-skipped`,
  `assertion-removed`), which `merge` cannot skip (it needs `mates diff` on
  the checked snapshot).
- [x] **Session row** (measured 2026-11-10): after `/new`, OpenCode's new
  `session` row appears only once the first message is sent, so `send` looks
  the session up after it pastes the message (Task 5, `send` step 7).
- [ ] **New session:** paste `/new` and Enter into the pane. Does OpenCode
  start a new session, and does `opencode.db` show it as the newest for that
  folder? If not, Task 5's `send` starts the new session by restarting the
  pane instead (see `send` step 5).
- [ ] **Empty input box:** from the three captures above, write the rule as
  a regex on the last lines of `capture-pane -p`. Task 5's `input_empty`
  implements exactly that rule.
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
- [ ] **Commit** the remaining answers to `docs/mates-probe.md` (first
  committed in ef25fb6 with the measured results above):
  "docs(mates): probe of OpenCode busy signal, new session, restore".

Probes that need Task 1 (OpenCode under read-only git) are run at the end of
Task 1.

### Task 1: `bin/qwen` additions

**Files:**
- Modify: `bin/qwen`, `bin/qwen-sandbox.md`, `docs/qwen.md`
- Test: `tests/test_qwen_bin.sh`

**Interfaces:**
- `qwen exec -- <cmd...>` runs `<cmd>` through `run_sandboxed` in the current
  directory (same mounts, no network), returns its exit code, and starts no
  TUI. It starts **no model server and no relay**: a check does not load the
  GPU, stop hosaka, or get a line to the model. `mates check` uses it.
  - `run_sandboxed` takes `-` as the host for this: it then starts no
    `qwen-relay out`, does not wait for the socket, and passes
    `--setenv QWEN_NO_RELAY 1` so the inner bash skips `qwen-relay in`.
  - Cleanup is one function, `cleanup`, set once with `trap cleanup EXIT`
    before anything it cleans exists; it kills the relay if one started,
    removes the run dir, and removes this run's lock file if one was
    written. No other `trap ... EXIT` is set.
- `QWEN_GIT_RO=1`: the whole shared git folder is bound **read-only**, and so
  is the worktree's `.git` file. Bind order matters (bwrap's later bind
  wins): after `--bind "$top" "$top"`, add `--ro-bind "$gitdir" "$gitdir"`
  (also when the git folder is inside `$top`), then
  `--ro-bind "$top/.git" "$top/.git"` when `.git` is a file. The worktree's
  own admin folder (`<common>/worktrees/<name>`, with its HEAD and index) is
  inside the shared folder, so it is read-only too. Qwen can run
  `git status` and `git diff` but cannot move a branch, write an object,
  stage a file, or repoint the worktree at another git folder. mates panes and `mates check` always set
  it. (Tested 2026-11-10 inside bwrap: status and diff work; writing a ref,
  editing or renaming `.git`, and `git add` all fail.)
- **OpenCode state per repo:** the sandbox gets
  `$STATE/opencode/<key>/` instead of the one shared folder, where `<key>`
  is the repo key (rule in Global Constraints) of the main checkout, the
  parent of `git rev-parse --path-format=absolute --git-common-dir`, so a
  repo's worktrees share it and no other repo's sessions are visible.
  `qwen opencode-dir` prints that folder for the current directory and
  exits; `mates` calls it instead of computing the key again, so the rule
  lives only in `bin/qwen`. The old shared `$STATE/opencode/opencode.db` is
  left where it is and not migrated (`docs/qwen.md` says so): moving old
  sessions into one repo's folder could show them to the wrong repo.
- **GPU lock:** each TUI run, OpenCode or `qwen aider` (not `exec`,
  `server`, `warm`), writes `$STATE/locks/<pid>` and `cleanup` removes it on
  exit. `$STATE` honours `XDG_STATE_HOME`, and so does Task 7's reader.
  Task 7's hosaka change reads these, so plain `qwen` and mates panes are
  both covered.
- `qwen host` prints the running server's address (`find_server` only,
  never starts one) or exits 1. Task 6 uses it for `answered_by`.
- `qwen warm` starts (or reuses) the server and loads the model in the
  background.
- `QWEN_RUNTIME=<dir>`: bind `<dir>` read-only at the same path and read
  `<dir>/env` in bash, keeping the lines that match
  `^[A-Za-z_][A-Za-z0-9_]*=` (the rule `runtime.runtime_env` uses; a test
  feeds both the same file). A `PATH=<v>` line becomes the sandbox PATH
  `<v>:/opt/opencode:$AIDER_VENV/bin:/usr/bin:/bin`; the other lines are
  `--setenv` after the defaults, so they win. Both come after the
  `SANDBOX_ENV` loop.
- `bin/qwen-sandbox.md`: `done.md` is two lines: what changed (files), and
  the test status as one of `passed`, `failed: <why>`, `could not run: <why>`.
  "Never install anything; if a tool is missing, write `could not run`."
- `sandbox-test` gains lines: with `QWEN_GIT_RO=1` in a worktree, writing a
  branch ref, writing an object and editing the `.git` file are `blocked`;
  another repo's OpenCode folder is not there.

- [ ] **Step 1: Failing tests** (`tests/test_qwen_bin.sh`, skipped without
  bwrap). Every test sets `XDG_STATE_HOME` and `XDG_CONFIG_HOME` to temp
  dirs and puts stubs first on `PATH`: `pgrep` (exit 1), `ollama` (sleeps),
  `curl` (exit 0), `systemctl` (no-op, so hosaka is never stopped) and
  `claude` (exit 1, so no digest call); `QWEN_OPENCODE` names a stub
  OpenCode that sleeps 2 s.
  - `exec` returns the command's exit code; `exec` cannot reach
    `/home/wai`; `exec` starts no `ollama` and no relay;
  - with `QWEN_GIT_RO=1`, `git update-ref refs/heads/x HEAD`, `git add` and
    `mv .git x` fail and `git status` works, in a main checkout and in a
    worktree;
  - `QWEN_RUNTIME` with `PATH=<dir>/bin` puts it first on PATH; bash and
    `runtime.runtime_env` keep the same lines of one sample `env` file;
  - a lock file exists while the stub TUI run is up and is gone after;
  - `qwen opencode-dir` gives the same folder in a repo and in its
    worktree, and a different one in another repo;
  - `qwen warm` starts the stub server and exits; `qwen host` exits 1 with
    no server.
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 4b: OpenCode under read-only git** (by hand, Wai watches): in a
  worktree of a scratch repo, `QWEN_GIT_RO=1 qwen`, give it the Task 0 toy
  task. Does OpenCode start, edit, and write `.mates/done.md` with no git
  error? Record the answer in `docs/mates-probe.md`. If it fails, stop and
  report before Task 2.
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
- `split_qwen(lead_pane: str, qwen_panes: list[str], cwd: str, cmd: str) -> list[str]`
  returns the full tmux argv for the next Qwen pane. `qwen_panes` are this
  repo's live Qwen pane ids, left to right (the caller sorts by
  `#{pane_left}`), so the last one is the rightmost:
  - the first Qwen pane: `tmux split-window -h -P -F '#{pane_id}' -t <lead> -l 50% -c <cwd> <cmd>`,
    so Claude keeps the left half;
  - later panes: the same with `-t <qwen_panes[-1]>` and no `-l`.
  - It never moves or resizes existing panes while you work.
- `split(argv) -> str` runs it and returns the new pane id; on "no space for
  new pane" it raises `NoRoom` and nothing changed.
- **Widths survive restore (as spire-trio does):** `pane_widths(panes) -> list`
  reads `#{pane_left} #{pane_width} #{window_width}` and returns
  `[{"pane": "lead"|n, "pct": int}]`, left to right. The state keeps that
  list as `"widths"` (Task 3); cli refreshes it before every save while the
  window is live. After restore has made all the splits (in the saved
  left-to-right order), `restore_widths(panes, widths)` resizes each pane
  except the last (`resize-pane -x <pct>%`), which takes the rest. That puts
  back what Wai set by hand; it is not re-tiling.

- [ ] **Step 1: Failing tests:**
  - `test_first_qwen_splits_lead_half`: argv contains `-h`, `-t %lead`, `-l 50%`.
  - `test_next_qwen_splits_rightmost`: with Qwen panes `%2,%3`, the argv has
    `-h -t %3` and no `-l`.
  - `test_split_argv_has_pane_id_cwd_cmd` (`-P -F '#{pane_id}'`, `-c`, the
    command last).
  - `test_restore_widths_reapplied` (stub tmux records `resize-pane -x 50%`,
    `-x 20%` for 3 panes saved at 50/20/30, and nothing for the last).
  - `test_pane_widths_left_to_right` (stub tmux listing out of order).
  - `test_split_no_room_raises` (stub tmux exits 1 with "no space for new
    pane": `NoRoom`). The message `add` prints is tested in Task 6.
  - `test_real_tmux_side_by_side` (skipped without tmux): in a detached
    `new-session -x 240 -y 60`, one lead plus 2 Qwen panes made with
    `split_qwen` + `split` all have the same top (side by side), and the
    lead is about 120 wide.
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
  `{"root", "claude_session": uuid, "claude_title": "mates <repo>", "parallel": PARALLEL_DEFAULT, "widths": [{"pane", "pct"}], "qwens": [{"n", "worktree", "branch", "opencode_session"|None, "closed": false, "task": {...}|None}]}`.
  `task` is `{id, summary|None, reason|None, files, base, direct, sent_ts|None, attempt_ts, attempts, opencode_session|None, questions: [{q, a}], diffed_tree|None, flags, checked_tree|None, check_cmd|None}`.
  `direct: true` marks Wai's own work with Qwen: `mates check` with no open
  task creates it (summary and reason `None`, base = merge-base with the
  lead branch, attempts 0), so `checked_tree` always lives in a task.
- `find_pane(root, n) -> (pane_id, "ok"|"failed")|None` and
  `find_lead(root) -> str|None`: look up by the `@mates` tag; `"failed"`
  when the pane has `@mates-failed=1`. `tag_pane(pane_id, root, n)` sets
  `@mates` and unsets `@mates-failed`.
- `mark_closed(state, n)`: called when a live lead's command finds pane N
  gone (`find_pane` is `None`). A failed pane (its `qwen` exited with an
  error; Task 4's wrapper) is reported as `failed`, never marked closed.
  Closed entries are skipped on restore; `add` reuses the lowest closed `n`
  and its worktree.
- `ensure_worktree(root, n) -> (path, branch)` creates or reuses
  `.claude/worktrees/mates-qwen-<n>` on `mates/qwen-<n>` from HEAD, and adds
  both exclude lines.
- `opencode_db(root) -> Path`: `$(MATES_QWEN opencode-dir)/opencode.db`, run
  in `root` (Task 1; the key rule is not repeated in Python).
- `opencode_session(worktree, db) -> str|None` returns the newest
  `session.id` with `directory = worktree` and no `parent_id`, from a
  read-only connection.
- `opencode_state(session, db, since) -> "busy"|"idle"`, the one place the
  busy rule lives (`send`, `check` and `wait` all call it):
  - `idle` if the newest message was created after `since` and is an
    assistant message with `time.completed` (as confirmed in Task 0);
  - `idle` if nothing in the session (message or `part`) was updated in the
    last 300 s and `since` is more than 300 s ago, so a crashed OpenCode
    stops counting as busy;
  - otherwise `busy`. Right after a paste, before any reply exists, that is
    `busy`, so `wait` cannot return `idle` at once.
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
    `session`, `message` and `part` columns Task 0 recorded)
  - `test_claude_session_follows_title` (two transcripts, newest titled wins)
  - `test_busy_expires_without_updates`
  - `test_busy_right_after_paste` (no message after `since`: `busy`)
  - `test_failed_pane_is_failed_not_closed` (stub tmux: the tag is there with
    `@mates-failed=1`)
  - `test_mark_closed_only_when_gone`, `test_tag_pane_sets_and_clears_failed`
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** "feat(mates): per-repo session state, panes found by tag".

### Task 4: Launcher

**Files:**
- Create: `bin/mates`, `mates/cli.py` (`launch`, `launch_plan`, `save_state`)
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
- `launch_plan(state: dict|None, root: str, fresh: bool, claude_args: list[str]) -> dict`
  is pure (no tmux, no git) and returns
  `{"claude_argv": [...], "qwens": [{"n", "worktree", "cmd"}], "warnings": [...], "state": <new state>}`.
  `cli.py launch [--fresh] [-- <args>]` calls it, does steps 1 to 5, and
  prints `claude_argv` NUL-separated on stdout; `bin/mates` reads it with
  `mapfile -d ''` and runs it as its child (step 6). Warnings go to stderr.
- `save_state(root, state)`: refreshes `state["widths"]` with
  `layout.pane_widths` while the window is live, then `session.save`.
  Every cli command saves through it.
- The plan, from the state:
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
    4. Split the Qwen panes (`split_qwen` + `split`, in the saved
       left-to-right order), tag each, and in each run a wrapper:
       `cd <worktree> && QWEN_GIT_RO=1 QWEN_RUNTIME=... qwen ...`. When
       `qwen` exits 0 the pane closes. On any other exit the wrapper sets
       the pane option `@mates-failed=1` (the tag stays), prints the error
       and leaves a shell, so the failure is seen and `send` refuses the
       pane. Then `restore_widths`.
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
  - **every brief names the exact symbols, files and check command**: Qwen
    guesses rather than asking (Task 0), so leave it nothing to guess, and
    read every `test-file` flag in the diff as a possible test bent to fit;
  - **never run code from a Qwen worktree yourself: use `mates check`**;
    a check must not depend on git state (git inside it sees the worktree);
  - `mates merge` needs `mates diff N` on the checked snapshot first;
  - the loop:
    `mates send N --reason "<why Qwen>" --files a,b "<task + files + how to check>"`
    (`--continue` keeps the OpenCode session, for a task that builds on what
    Wai and Qwen just discussed),
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
  - `test_launch_plan_passes_claude_args` (`-- --model x` ends `claude_argv`)
  - `tests/test_launcher.sh`, each a detached tmux with stub `claude`/`qwen`
    (`MATES_CLAUDE`, `MATES_QWEN`):
    - `test_one_pane_window_reused`
    - `test_two_pane_window_gets_new_window`
    - `test_second_mates_switches_to_first`
    - `test_exited_lead_restarted`
    - `test_failing_claude_leaves_shell`
  - `test_failed_qwen_pane_not_closed` (stub `qwen` exits 1: the pane stays,
    tagged with `@mates-failed=1`, and the entry is not closed)
  - `test_fresh_keeps_worktrees` (a dirty worktree is warned about, not reset)
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** "feat(mates): launcher with fresh/restore". Wai links `~/bin/mates`.

### Task 5: Delegation: send, wait, answer, check, diff, sync, merge, discard

**Files:**
- Create: `mates/gitsafe.py`
- Modify: `mates/cli.py`, `mates/guard.py`
- Test: `tests/test_gitsafe.py`, `tests/test_cli.py`, `tests/test_guard.py`

**Interfaces:**
- `gitsafe.git_safe(root, n, *args, index=None, check=True, input=None) -> subprocess.CompletedProcess`
  runs git on Qwen worktree N without letting git find anything through
  Qwen's folder:
  - `common` = `git -C <root> rev-parse --path-format=absolute --git-common-dir`
    (the lead's checkout: trusted). The admin folder is the entry under
    `<common>/worktrees/` whose `gitdir` file names `<worktree>/.git`;
    none found is an error. Both are read-only to Qwen (Task 1).
  - env: `GIT_DIR=<admin folder>`, `GIT_COMMON_DIR=<common>`,
    `GIT_WORK_TREE=<worktree>`, `GIT_INDEX_FILE=<index>`, where `index` is
    the caller's temp index path or, if `None`, a fresh temp file deleted
    afterwards; `cwd` is the worktree.
  - argv: `git -c core.hooksPath=/dev/null -c core.fsmonitor=false <args>`;
    when `args[0]` is `diff`, `log` or `show`, `--no-ext-diff --no-textconv`
    go right after it.
  - text mode; `check=True` raises `GitError(stderr)` on non-zero.
- `gitsafe.git_lead(root, *args, check=True)`: the same `-c` flags and diff
  flags, normal repo discovery in `root` (the lead's own checkout).
- Shared helpers in `cli.py`, used by every command below:
  - `snapshot(root, n, base) -> tree`: a temp index seeded with
    `read-tree <base>`, then `add -A -- . ':!.mates'`, then `write-tree`
    (`git_safe`). Tracked files under `.mates/` keep their base version;
    Qwen's edits there and untracked `.mates/*` never reach the tree.
  - `worktree_dirty(root, n) -> list[str]`: paths where `snapshot(root, n,
    HEAD)` differs from the worktree's HEAD tree; empty means clean.
  - `paste(pane, text)`: `load-buffer -b mates-<n> -`, then
    `paste-buffer -p -d -b mates-<n> -t <pane>`, then `send-keys -t <pane> Enter`
    (Task 0's measured recipe).
  - `input_empty(pane) -> bool`: Task 0's regex on `capture-pane -p`.
  - `log_attempt(root, entry, outcome)`: builds and appends the log line
    (Task 5 fills the fields it has; Task 6 adds the cost fields).
- `mates send N --reason "<why Qwen>" [--files a,b] [--continue] "<text>"`:
  1. Refuse if pane N is missing (and mark it closed), if it is failed, if
     the pane is not running the sandbox (`#{pane_current_command}`), if
     OpenCode is busy, if `input_empty` is false, if another pane has an
     open task and `parallel` is false, or if `worktree_dirty` is non-empty
     (message names `merge` and `discard`).
  2. Rebase `mates/qwen-N` onto the lead HEAD (`git_safe`).
  3. Delete `.mates/done.md` and `.mates/question.md`.
  4. Record the task in the state (id, summary, reason, files, base, ts,
     attempts).
  5. Start a new OpenCode session (`paste(pane, "/new")`, as Task 0
     confirmed; if Task 0 found `/new` does not work, `respawn-pane -k` the
     pane with the Task 4 wrapper and plain `qwen` instead), so the task
     gets a clean context. `--continue` skips this, for a task that builds
     on what Wai and Qwen just discussed.
  6. `paste(pane, "[from Claude] <text>")`.
  7. Only now look the session up (its row appears with the first message,
     Task 0): poll `opencode_session(worktree, db)` for up to 30 s until it
     differs from the stored one, and store the new id in the entry and the
     task. If none appears yet, the task's `opencode_session` stays `None`
     and `wait` repeats the lookup on every poll, staying `busy` meanwhile.
     With `--continue` the stored id is kept.
  8. If attempts ≥ 2, print a reminder to discard and escalate.
- `mates wait N [--timeout S]` (default 1800 s) prints one line and exits:
  `question: <text>`, `done: <text>`, `dead` (pane gone; marked closed),
  `failed` (`@mates-failed=1`: `qwen` exited with an error),
  `idle` (`opencode_state(..., since=attempt_ts)` is `idle` and neither
  file exists), or `timeout`. Qwen's text is cut at 2,000 characters.
  It's built for a Monitor until-loop.
- `mates answer N [--sendback] "<text>"` (`--sendback` is a plain flag;
  the text is the positional) deletes **both** files,
  `paste(pane, "[from Claude] <text>")` into the same OpenCode session, and
  records the Q&A in the task. With `--sendback` it counts an attempt,
  `log_attempt(..., "sent-back")`, sets `attempt_ts`, and clears
  `checked_tree` and `diffed_tree`.
- `mates check N -- <cmd>`:
  1. Refuse if OpenCode is busy (Qwen could edit the check folder mid-run).
     With no open task, create the `direct` task (Task 3).
  2. `tree = snapshot(root, n, base)`.
  3. Unpack that tree into `<worktree>/.mates/check/<tree>/` with
     `git_safe`: `read-tree <tree>` into a temp index, then
     `checkout-index -a -f --prefix=<dir>/` (not `git archive`, which would
     obey `export-ignore` in a Qwen-edited `.gitattributes`). An older check
     folder is removed first.
  4. Run `<cmd>` there with `MATES_QWEN exec -- sh -c <cmd>`
     (`QWEN_GIT_RO=1`, the runtime). The check folder is not a repo of its
     own: `git` inside a check sees the worktree, read-only, so a check
     must not depend on git state (`lead.md` says so).
  5. Print the exit code and the last 20 lines. On exit 0, record the tree
     as `checked_tree` and the command as `check_cmd`.
  The check runs on the snapshot, so nothing Qwen does to the folder
  afterwards, and nothing the check leaves behind, can reach the merge.
- `mates diff N` prints, with `git_safe`: the full diff of
  `snapshot(root, n, base)` against the task base (so untracked files are
  in it), a diff stat, and `guard_flags`. It records the snapshot tree as
  `diffed_tree` and the flags in the task.
- `guard.guard_flags(diff_text, brief_files, hooks_path=None, runtime_files=())`
  (new keyword arguments; old callers unchanged). `cli` passes the lead
  repo's `git config core.hooksPath` and the files `runtime.runtime_hash`
  covers (`.mates/runtime.sh` and its `# lockfiles:` names, read from the
  base). New flags:
  - `hook-path:<path>` for any path under `.githooks/`, `.husky/`,
    `hooks_path`, or any `.git*` file;
  - `runtime-input:<path>` for any of `runtime_files` plus the common
    manifests and lockfiles (`pyproject.toml`, `uv.lock`,
    `requirements*.txt`, `package.json`, `package-lock.json`,
    `Cargo.toml`, `Cargo.lock`), because the next launch rebuilds the
    runtime from them as Wai, with network.
- `mates merge N [-m "<summary>"]`:
  1. Refuse unless there is a `checked_tree`, `diffed_tree` equals it
     (`run mates diff N first`: the lead read the diff of exactly what
     merges), a `direct` task has `-m`, the lead checkout is on a
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
     if `snapshot(root, n, base)` now differs from `checked_tree` (Qwen kept
     editing), commit it to `refs/mates/discarded/<n>-<ts>` first and say
     so.
  5. `log_attempt(..., "accepted")`, then clear the task.
  Qwen cannot commit (git is read-only for it), so there are no Qwen commits
  to fold.
- `mates sync N`, for when the lead branch moved after the task was sent:
  1. `snapshot(root, n, base)` as a commit on the old base (`commit-tree`).
  2. Cherry-pick it onto the lead HEAD in a scratch index
     (`merge-tree --write-tree`), touching no folder.
  3. On a conflict: print `conflict` and the paths, and change nothing. The
     lead resolves by sending the task back with notes, or discards.
  4. Otherwise check the merged tree out into the worktree, set the task
     base to the lead HEAD, and clear `checked_tree`, so the check must run
     again on what will merge.
- `mates discard N`: `snapshot(root, n, base)` as a commit on
  `refs/mates/discarded/<n>-<ts>`, reset the worktree to the lead HEAD,
  `log_attempt(..., "escalated")` (or `failed` with `--failed`), clear the
  task. Nothing is deleted; the snapshot ref stays.

- [ ] **Step 1: Failing tests** (stub tmux records buffers and `send-keys`; `MATES_QWEN` names a stub whose `exec` records its argv and runs the command; temp repos):
  - `test_git_safe_env_and_flags` (`GIT_DIR`, `GIT_COMMON_DIR`,
    `GIT_WORK_TREE`, `GIT_INDEX_FILE` set; `--no-ext-diff --no-textconv`
    after `diff`)
  - `test_send_pastes_prefixed_message` (one paste, then one Enter)
  - `test_send_refuses_dirty_worktree`, `test_send_refuses_busy`,
    `test_send_refuses_second_open_task`, `test_send_refuses_shell_pane`,
    `test_send_refuses_nonempty_input`, `test_send_refuses_missing_pane`
    (and marks it closed), `test_send_refuses_failed_pane`
  - `test_send_rebases_clean_worktree`
  - `test_send_starts_new_session` (`/new` is pasted before the message, the
    session is looked up after it, the new id is stored; `--continue`
    pastes no `/new`)
  - `test_wait_finds_late_session_row` (no row at send time: `wait` stays
    busy, then picks the id up)
  - `test_send_reminds_after_two_attempts`
  - `test_wait_done`, `test_wait_question`, `test_wait_dead_pane`,
    `test_wait_failed_pane`, `test_wait_idle`, `test_wait_not_idle_right_after_send`,
    `test_wait_timeout`
  - `test_answer_clears_stale_done`, `test_answer_sendback_counts_attempt`
    (attempt counted, `sent-back` logged, check cleared)
  - `test_diff_includes_untracked`
  - `test_check_cannot_reach_home` (real `bin/qwen exec`, skipped without
    bwrap: a check that writes `$HOME/canary` and reads `~/.ssh` fails, and
    no canary appears)
  - `test_check_refuses_busy`
  - `test_check_ignores_export_ignore` (a Qwen-added `export-ignore` line
    does not drop a file from the check folder)
  - `test_check_runs_on_snapshot` (a file the check writes is not in the
    worktree's tracked or untracked set outside `.mates/`)
  - `test_merge_fast_forwards_and_resets`
  - `test_merge_refuses_without_check`, `test_merge_refuses_without_diff`
  - `test_merge_logs_accepted`
  - `test_merge_commits_checked_snapshot` (the folder is edited after the
    passing check: the merged commit has the checked tree, and the later
    edit is kept on a `refs/mates/discarded/` ref)
  - `test_merge_refuses_when_lead_moved`
  - `test_sync_carries_work_and_clears_check`,
    `test_sync_conflict_changes_nothing`
  - `test_merge_refuses_dirty_lead_paths`, `test_merge_refuses_detached_lead`
  - `test_merge_without_task_uses_merge_base` (and refuses without `-m`)
  - `test_merge_keeps_tracked_mates_files` (a tracked `.mates/runtime.sh`
    is in the merged tree at its base version; Qwen's edit to it and its
    `done.md` are not)
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
  splits (side by side, `split_qwen` + `split`) and tags a Qwen pane, and
  saves. On `NoRoom` it prints "no room for another Qwen pane: widen the
  window or close a pane" and changes nothing.
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
  `failed`), written by `log_attempt` (Task 5), with these filled in:
  - `repo`, `task_id`, `summary`, `route` = `qwen`, `reason` (from `send
    --reason`), `attempts`, `outcome`, `pane` = n (new field);
  - `check` = `check_cmd` and `check_sha` = its sha256 (as the pilot does),
    `tree` = `checked_tree`;
  - `flags` from `mates diff`, and `full_diff_read` = (`diffed_tree` ==
    `checked_tree`);
  - `questions`: the Q&A list (new field);
  - `worker_wall_s`: this attempt's start to its finish; `worker_tokens`:
    the sum of `tokens.input + tokens.output` (the names Task 0 recorded) of
    the task's OpenCode session's assistant messages created since
    `attempt_ts` (so send-backs are not counted twice);
  - `answered_by`: `[<model>]`, a one-item list as the pilot writes it: the
    model `/api/ps` reports loaded at the finish, on the address
    `MATES_QWEN host` prints; `null` if there is no server;
  - `lead_tokens(transcript, n, since) -> (int, int)`: over the lead
    transcript (`claude_session`'s file, JSONL), the assistant entries after
    `since` whose `tool_use` Bash command runs `mates <sub> N` for this
    pane; `lead_tokens` = the sum of `usage.input_tokens +
    cache_creation_input_tokens + output_tokens`, and `lead_cache_read` (an
    existing field) = the sum of `cache_read_input_tokens` (the spec's rule;
    rough, within a factor of 2). Other work the lead did while waiting is
    not counted.
  - `logrec.FIELDS` gains `questions` and `pane`. `tree` and
    `full_diff_read` already exist (in `PILOT_FIELDS`, which `validate`
    accepts) and are reused. `red_on_base` and `verdict` stay in `FIELDS`,
    unfilled, so old pilot lines still validate.

- [ ] **Step 1: Failing tests:**
  - `test_add_then_restore_has_two`
  - `test_add_reuses_closed`
  - `test_add_no_room_message` (stub tmux exits 1 with "no space for new
    pane": the message, and the state unchanged)
  - `test_remove_refuses_dirty`, `test_remove_force_snapshots`,
    `test_remove_is_only_drop`
  - `test_list_shows_failed_pane`
  - `test_log_filters`, `test_log_route_appends`
  - `test_log_line_has_cost_fields` (fixture OpenCode DB and transcript,
    stub `qwen host` and `/api/ps`)
  - `test_lead_tokens_counts_only_this_pane`
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

**Files:**
- Modify: the hosaka module that starts the server (find it with
  `grep -rn 'hosaka-server\|def .*serve' ~/src/hosaka`; name it in the
  commit body)
- Test: that module's test file in hosaka's suite

**Interfaces:**
- hosaka's server start refuses, with "qwen is using the GPU (<n> session(s));
  close them or run `qwen stop`", while any
  `${XDG_STATE_HOME:-~/.local/state}/qwen/locks/<pid>` names a live pid. Dead pids are stale:
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
- Modify: `mates/cli.py`, `mates/lead-lessons.md` (rewritten by each retro),
  `OPEN-QUESTIONS.md` (gitignored; proposals appended)
- Test: `tests/test_cli.py`

**Interfaces:**
- `mates retro [--date D]`:
  1. Skip a day with no log lines.
  2. Build the input: that day's log lines with **the question text
     removed** (the count stays) and each flag cut to its kind
     (`unnamed-file:<path>` becomes `unnamed-file`; paths are Qwen's
     choice), plus the last 7 retros. Those are the only parts of a log line
     Qwen wrote; `summary`, `reason`, `check` and the answers are the lead's
     (or Wai's) own words, and the retro needs them to find misroutes and
     weak briefs.
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
- The service: `ExecStart=%h/bin/mates retro`,
  `Environment=PATH=%h/bin:%h/.local/bin:/usr/bin:/bin` (systemd does not
  expand `~`; `%h` is the home folder), so `claude` is found.
- The timer: `OnCalendar=*-*-* 04:30:00` with `Persistent=true` (user lingering is on).
  Wai: "daily retro should run at 4:30 AM if the computer is on, if not it should run
  as soon as it can". `Persistent=true` makes a missed 4:30 run fire at the next
  start of the user systemd (WSL started, or the machine booted). With no `--date`,
  the retro covers every day since the last retro file, up to yesterday, so a
  late or skipped run loses nothing.

- [ ] **Step 1: Failing tests** (stub claude):
  - `test_retro_skips_empty_day`
  - `test_retro_input_has_no_question_text` (summary and reason are kept)
  - `test_retro_input_flags_have_no_paths`
  - `test_retro_rejects_lesson_with_repo_name`,
    `test_retro_allows_slash_words`
  - `test_retro_caps_lessons` (a 201-character lesson is rejected; only the
    first 20 passing ones are kept)
  - `test_retro_writes_findings_and_rejects` (the retro file has
    `findings_md` and the rejected lessons)
  - `test_retro_appends_proposals` (under `## Open`, nothing applied)
  - `test_retro_lessons_replace_file`
  - `test_retro_commits_only_lessons_on_main`,
    `test_retro_no_commit_when_tree_dirty`
  - `test_retro_catches_up_missed_days`
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5:** Wai enables the timer (`systemctl --user enable --now mates-retro.timer` after linking).
- [ ] **Step 6: Commit** "feat(mates): daily retro".
