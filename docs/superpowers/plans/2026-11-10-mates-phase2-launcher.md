# Mates Phase 2 (the `mates` command) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `mates` command, a Claude lead in the left half of a tmux
window that spawns sandboxed Qwen workers in panes on the right, reviews their
work through fixed fields, and logs every routing decision. Also the hosaka
lock and the daily retro.

**Architecture:**
- **One Python CLI, `mates/cli.py`, behind `bin/mates`.** Each subcommand is
  a function over a per-task state file, with no daemon. The lead (Claude)
  drives each step and waits on files with a Monitor.
- **Workers are interactive Claude Code on Qwen,** in tmux panes, inside the
  Phase 1 sandbox (`sandbox.sh`, run as user `claude` under bwrap).
- **Review reuses the Phase 1 pieces** (`collect`, `verify`, `rerun`, guard
  flags, the forwarder).
- **The Ollama server is the one `qwen` uses,** found by `qwen server`. No
  fixed port.

**Tech Stack:** Python 3.12 stdlib, bash, tmux 3.4, bwrap 0.9.0, Ollama 0.40.16, Claude Code 2.1.322 (pinned for workers).

**Spec:** `docs/superpowers/specs/2026-11-10-claude-qwen-mates-design.md`.
Read "How it works", "Layout", "Two commands", "Settled after the builder
review", "Audit log", and "Daily retrospective". The Phase 1 plan and its
ledger hold the measured facts this plan relies on.

## Global Constraints

- **Wai skipped the pilot gate (2026-11-10, "c").** The Phase 1 safety pieces
  still apply unchanged: the sandbox as user `claude`, the read-only original
  check, red on base, guard flags, the blind verifier, the authoritative
  rerun.
- The commands are exactly **`mates`** and **`qwen`**. Model names stay
  accurate, with no aliases.
- Plain `claude` stays untouched: no global CLAUDE.md pointer, no global
  skill.
- `mates <subcommand>` refuses unless `MATES_LEAD=1`.
- **One worker at a time.** A second spawn queues. Two at once measured
  0.4-0.5 tok/s.
- Never `--dangerously-skip-permissions`.
- **Wai: "Qwen wins vs TTS".** Spawning stops hosaka; hosaka refuses to start
  while `~/.claude/mates/running/*` exists.
- **What merges is what was checked.** Accept refuses if the tree differs
  from the one that passed the rerun.
- **Layout:**
  - the lead pane is pinned to 50% of the window, on the left;
  - workers tile the right half in a grid of c columns by r rows;
  - the grid is the one whose pane cols/rows is closest to 0.92, compared in
    log space.
- **Retro rules:** the retro writes `~/.claude/mates/retros/YYYY-MM-DD.md`.
  - It may edit only `mates/lead-lessons.md`, task types only, with no repo
    names.
  - It proposes rule, concurrency or model changes in `OPEN-QUESTIONS.md`; it
    never applies them.

## Review Focus

1. **Worker pane dies before writing its report** (crash, OOM, the user
   closing it). Expected: `mates wait` returns `dead` instead of hanging. Test
   in Task 4 (`test_wait_reports_dead_pane`).
2. **Lead branch moved since spawn.** Expected: accept rebases. On conflict it
   refuses with a message and leaves both branches intact. Test in Task 5
   (`test_accept_conflict_refuses`).
3. **`mates` run inside a window that already has panes.** Expected: it opens
   a new window, never splits someone's layout. Test in Task 6
   (`test_launch_window_choice`).
4. **Queue head whose base commit no longer exists or isn't an ancestor.**
   Expected: the start fails cleanly and is logged `failed`. Test in Task 3
   (`test_queue_start_bad_base`).
5. **Stale `running/` lock after a crash, so hosaka never starts.** Expected:
   `mates list` shows it, and `mates close --stale` clears locks whose pane is
   gone. Test in Task 7 (`test_stale_lock_cleared`).

---

## File map

| File | Responsibility |
|---|---|
| `mates/layout.py` | `pick_grid`, tmux geometry and apply |
| `mates/taskstate.py` | per-task state JSON, queue, running locks |
| `mates/cli.py` | subcommands: spawn, wait, review, accept, send-back, escalate, close, list, log, layout, retro |
| `mates/server.py` | find or start the Ollama server via `qwen server` |
| `mates/lead.md` | the lead role prompt |
| `mates/lead-lessons.md` | routing lessons the retro maintains |
| `mates/prompts/retro.md` | retro prompt |
| `bin/mates` | launcher (tmux, lead) plus dispatch to `cli.py` |
| `deploy/mates-retro.{service,timer}` | systemd user units |
| `~/src/hosaka` | lock check before the server starts (separate repo commit) |

---

### Task 1: Server discovery, no fixed port

**Files:**
- Create: `mates/server.py`
- Modify: `mates/pilot.py` (`start_fwd` upstream), `mates/ctx.py` (`PORT`)
- Test: `tests/test_server.py`

**Interfaces:**
- `ollama_host() -> str` runs `qwen server` (resolved via `$HOME/bin/qwen`,
  then `PATH`) and returns `host:port`. It raises `RuntimeError` on failure.
- `pilot.start_fwd(sock, log, model)` uses `ollama_host()` for upstream.

- [ ] **Step 1: Failing tests**
  - `test_ollama_host_parses_qwen_output`: a stub `qwen` printing
    `127.0.0.1:4242` returns `127.0.0.1:4242`.
  - `test_ollama_host_error`: a stub exiting 1 raises `RuntimeError`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** The stub is found via the `MATES_QWEN` env
  override. Replace the literal `127.0.0.1:11435` in `pilot.start_fwd`.
- [ ] **Step 4: Run the suite, expect PASS.**
- [ ] **Step 5: Commit** "feat(mates): find the model server via qwen, no fixed port".

### Task 2: Layout

**Files:**
- Create: `mates/layout.py`
- Test: `tests/test_layout.py`

**Interfaces:**
- `pick_grid(cols: int, rows: int, n: int, target=0.92) -> tuple[int, int]`
  takes the right half's size in cells and returns `(c, r)` with `c*r >= n`.
  It minimizes `abs(log((cols/c)/(rows/r)) - log(target))`; ties go to fewer
  empty cells.
- `apply(window: str, lead_pane: str, worker_panes: list[str]) -> None`:
  - resizes the lead to 50% of the width;
  - arranges the workers into the grid with `tmux split-window`,
    `resize-pane` and `select-layout` (no `move-pane` across windows);
  - is idempotent.

- [ ] **Step 1: Failing tests** (spec examples):
  - `test_one_worker_fills_half`: `pick_grid(120,60,1) == (1,1)`.
  - `test_two_side_by_side`: `pick_grid(120,60,2) == (2,1)`.
  - `test_four_workers`: `pick_grid(120,60,4) == (2,2)`.
  - `test_tall_window`: `pick_grid(60,80,2) == (1,2)`.
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement `pick_grid`** with an
  exhaustive search over `c in 1..n`.
- [ ] **Step 4: `apply` against a real tmux.**
  `test_apply_real_tmux` (skipped if there's no tmux) starts a detached
  `tmux new-session -d -x 240 -y 60` and creates 2 workers. It expects the
  lead width to be 120±1, the workers side by side, then kills the session.
- [ ] **Step 5: Commit** "feat(mates): phone-shaped worker grid".

### Task 3: Task state, queue, running locks

**Files:**
- Create: `mates/taskstate.py`
- Test: `tests/test_taskstate.py`

**Interfaces:**
- Root: `STATE = ~/.claude/mates`. A task is `tasks/<id>/state.json` with keys:
  - `id`, `repo`, `lead_branch`, `base`, `brief`, `check`, `status`;
  - `status` is one of `queued`, `running`, `reported`, `reviewing`,
    `accepted`, `sent-back`, `escalated`, `failed`, `closed`;
  - `attempt`, `worker_pane`, `verifier_pane`, `commit`, `tree`,
    `checked_tree`, `flags`, `verdict`, `spawned_ts`, `log_fields`.
- `new_id(repo) -> str` gives `<reponame>-<YYYYmmdd-HHMMSS>-<4 hex>`.
- `load(id) -> dict` and `save(state)`, which writes a temp file then renames
  it.
- `enqueue(id)` and `pop_queue() -> id|None` use `queue` (one id per line,
  locked with `fcntl`).
- `lock(id)` / `unlock(id)` manage `running/<id>`; `running() -> list[str]`.

- [ ] **Step 1: Failing tests:**
  - `test_save_load_roundtrip`
  - `test_queue_fifo`
  - `test_lock_unlock`
  - `test_queue_start_bad_base`: a state whose base isn't in the repo makes
    `cli.start_queued()` log `failed`. This test lands in Task 4.
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** "feat(mates): task state, queue, locks".

### Task 4: `spawn` and `wait`

**Files:**
- Create: `mates/cli.py`
- Modify: `mates/sandbox.sh` (add `--interactive`)
- Test: `tests/test_cli.py` (with stub tmux, sandbox and claude, as in Phase 1)

**Interfaces:**
- `mates spawn <brief.json> --check "<cmd>" [--green-base "<why>"] [--inject src:dest]... [--files a,b]`
  prints the id.
  1. Validate the brief (`guard.load_brief`).
  2. Run red on base in the sandbox (refuse or log as in Phase 1).
  3. `git worktree add` on branch `mates/<id>` from the lead's committed HEAD;
     warn if the brief's files are dirty.
  4. If another task is `running`: `enqueue`, print `queued <id>`, stop here.
  5. Stop hosaka (`systemctl --user stop hosaka-server`, logged), `lock(id)`,
     start the forwarder.
  6. Open the worker pane with `tmux split-window`, then `layout.apply`. The
     pane runs `sandbox.sh --interactive ... -- claude-2.1.322 --permission-mode acceptEdits --append-system-prompt <worker.md> "<brief text>"`.
- `sandbox.sh --interactive` drops `--new-session`, so the TUI keeps its
  terminal. That's safe here because `/proc/sys/dev/tty/legacy_tiocsti` is
  `0`, so no keystroke injection. It refuses to start if that file isn't `0`.
- `mates wait <id> [--timeout S]` blocks until `.mates/report.md` exists
  (prints `reported`) or the worker pane is gone (prints `dead`, status
  `failed`). It's meant to run as the lead's Monitor command.

- [ ] **Step 1: Failing tests:**
  - `test_spawn_refuses_green_base`
  - `test_spawn_queues_when_busy`
  - `test_spawn_opens_pane_with_sandbox_interactive` (stub tmux records argv)
  - `test_wait_reports_report`
  - `test_wait_reports_dead_pane`
  - `test_spawn_stops_hosaka` (stub systemctl)
  - `test_interactive_refused_without_tiocsti_off` (env
    `MATES_TIOCSTI_FILE` points at a file holding `1`)
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5: Real check.**
  1. In a scratch repo inside a detached tmux session, spawn the Phase 1
     probe task.
  2. `wait` returns `reported` within 10 min.
  3. `capture-pane` shows the Claude Code TUI.
- [ ] **Step 6: Commit** "feat(mates): spawn and wait".

### Task 5: `review`, `accept`, `send-back`, `escalate`

**Files:**
- Modify: `mates/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- `mates review <id>` uses `pilot.collect`, then `pilot.verify` (the verifier
  runs headless in its own pane under `tmux` with `-p`, its output visible),
  then `pilot.rerun` with the original check. It sets `checked_tree` and
  prints one JSON object of fixed fields: `goal, verdict_text, diff_stat,
  flags, rerun, check_tail, attempt`.
  - The lead reads the full diff only via `mates diff <id>`. A flag or a
    failing verdict tells it to.
- `mates diff <id>` prints `git diff base..commit`.
- `mates accept <id>`:
  1. Rebase `mates/<id>` onto the lead's current branch HEAD, in the
     worktree.
  2. Refuse if the rebased tree differs from `checked_tree`; rerun `review`
     first if the base moved.
  3. `git merge --ff-only mates/<id>` in the lead's checkout.
  4. Log `accepted`, then `close`.
  - On a rebase conflict: abort the rebase, print `conflict`, change nothing.
- `mates send-back <id> "<notes>"`: `attempt += 1`. With attempt > 2 it
  refuses and prints "escalate". Otherwise it removes the old report and
  types the notes into the worker pane (`tmux send-keys -l` then `Enter`).
- `mates escalate <id> "<why>"` logs `escalated` and closes. The lead then
  does the task itself.

- [ ] **Step 1: Failing tests:**
  - `test_review_prints_fixed_fields`
  - `test_accept_fast_forwards`
  - `test_accept_refuses_changed_tree`
  - `test_accept_conflict_refuses`
  - `test_send_back_types_notes` (stub tmux)
  - `test_third_attempt_refused`
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** "feat(mates): review, accept, send-back, escalate".

### Task 6: `close`, `list`, `log`, `layout`, and the `mates` launcher

**Files:**
- Modify: `mates/cli.py`
- Create: `bin/mates`, `mates/lead.md`, `mates/lead-lessons.md`
- Test: `tests/test_cli.py`, `tests/test_launcher.sh`

**Interfaces:**
- `mates close <id> [--stale]`:
  - kills the task's panes and removes its worktrees (`fsutil.rmtree`);
  - unlocks, writes the final log line (with `lead_tokens` estimated, see
    below);
  - starts the next queued task;
  - restarts hosaka if nothing is running;
  - reapplies the layout.
  - `--stale` closes every `running` task whose worker pane is gone.
- `mates list` prints a table: id, status, attempt, pane, age.
- `mates log [--repo R] [--route X] [--outcome Y]` prints a table from
  `log.jsonl`.
- `mates layout` reapplies the grid.
- **`lead_tokens` estimate:** in the lead's transcript (the newest
  `~/.claude/projects/<mangled lead cwd>/*.jsonl`), sum `usage` (input,
  output and cache-creation tokens) of assistant turns whose tool input or
  text contains the task id.
- **`bin/mates`** with no subcommand launches:
  - **Window choice:** outside tmux, `tmux new-session -s mates` re-running
    `bin/mates`. Inside tmux, use the current window if it has one pane,
    else `tmux new-window` re-running `bin/mates`.
  - **Warm start:** a background `qwen server` plus a 1-token request to
    load the model.
  - **The lead itself:**
    `exec env -u ANTHROPIC_API_KEY MATES_LEAD=1 command claude --append-system-prompt "$(cat mates/lead.md mates/lead-lessons.md)" "$@"`.
  - With a subcommand, it runs `python3 mates/cli.py "$@"`, refusing without
    `MATES_LEAD=1`.
- **`lead.md`** contains:
  - the routing rule, quoted from the spec;
  - the corollary: no check, no Qwen;
  - the brief template (JSON keys `goal`, `files`, `constraints`);
  - the loop: spawn, then a Monitor on `mates wait`, then review, then diff
    if flagged, then accept / send-back / escalate;
  - "never read the worker's transcript";
  - "log Claude-only routes too": `mates log-route claude-self "<summary>" "<reason>"`.

- [ ] **Step 1: Failing tests:**
  - `test_close_starts_next_queued`
  - `test_stale_lock_cleared`
  - `test_log_filters`
  - `test_lead_tokens_estimate` (a fixture transcript)
  - `test_subcommand_refused_without_lead_env`
  - `tests/test_launcher.sh::test_launch_window_choice` (detached tmux:
    one-pane window reused, a two-pane window gets a new window)
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement**, plus `mates log-route`.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Real end-to-end check.**
  1. In a scratch repo under tmux, run `bin/mates`.
  2. The lead is told to fix the probe task.
  3. Watch it spawn, wait, review and accept, and see the log line appear.
  4. This step runs Claude, and Wai watches it.
- [ ] **Step 6: Commit** "feat(mates): launcher, close/list/log/layout, lead role". Link `~/bin/mates` (Wai runs `ln -sf`).

### Task 7: Hosaka lock

**Files:**
- Modify: `~/src/hosaka/scripts/gpu_mode.sh`, plus the hosaka server unit's
  `ExecStartPre` (hosaka repo)

**Interfaces:**
- Before `hosaka-server` starts: if `~/.claude/mates/running/` has entries
  whose pane still exists, it exits non-zero with "mates worker running
  (<ids>); `mates close` first".

- [ ] **Step 1: Failing test in the hosaka repo** (its pytest style):
  `test_start_refused_with_mates_lock`, using a temp `HOME` with
  `running/x`. Also `test_stale_lock_cleared` in mates (Task 6).
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement** as a small script,
  `scripts/mates_lock_check.sh`, called from `ExecStartPre` and from
  `gpu_mode.sh homo`.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit in hosaka**, by path, with the repo's own conventions.

### Task 8: Daily retro

**Files:**
- Create: `mates/prompts/retro.md`, `deploy/mates-retro.service`, `deploy/mates-retro.timer`
- Modify: `mates/cli.py` (`retro`)
- Test: `tests/test_cli.py`

**Interfaces:**
- `mates retro [--date D]` (allowed without `MATES_LEAD`):
  1. Skip if there are no log lines for the date.
  2. Otherwise run `claude -p --setting-sources project --tools Read,Edit --strict-mcp-config`
     from `/srv/mates/neutral`. It gets the day's `log.jsonl` lines and the
     last 7 retros, never worker reports or transcripts.
  3. Write `retros/D.md`.
  4. Allow edits only to `mates/lead-lessons.md`; verify after the run with
     `git diff --name-only`, and revert anything else.
  5. Commit lessons changes, dated.
- The timer runs at 23:30 daily (`Persistent=true`).

- [ ] **Step 1: Failing tests:**
  - `test_retro_skips_empty_day`
  - `test_retro_reverts_disallowed_edits` (stub claude edits two files)
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5:** Wai installs the timer: `systemctl --user enable --now mates-retro.timer` after linking the units.
- [ ] **Step 6: Commit** "feat(mates): daily retro".
