# Claude-led crew with local Qwen workers: design

Date: 2026-11-10. Status: draft for Wai's review.

## Intent

Wai is on a Claude Max subscription. The goal is to **get more work out of the
same usage limits by pushing everything that can be checked onto the local
GPU.** Claude stays the lead: it diagnoses, coordinates, comes up with ideas,
picks who does each task, and reviews. A local Qwen3.8-27B does the mechanical
work in visible tmux panes. This is global: it works in any repo, not only the
tower defense game that prompted it.

The shape comes from `~/bin/spire-trio` (`~/.claude/spire-trio.sh`): peer
Claude sessions in one tmux window that talk over ListAgents/SendMessage. The
difference here is that the lead **spawns workers on demand**, and the workers
run Qwen, not Claude.

## Routing rule (Wai's)

> "hand everything with a clear auditable outcome to Qwen / complex reasoning
> with divergent outcomes go to Claude"

- **Qwen:** a task with a pass/fail check: tests pass, build is clean, a grep
  or diff matches the brief, values come out as stated. Examples: mechanical
  edits, renames, wiring up an existing pattern, boilerplate and tests, docs
  from a given outline.
- **Claude** (the lead itself, or a Claude subagent): unclear bugs, design,
  mechanic ideas, tradeoffs, anything where reasonable answers differ.
- **Corollary (Claude's call):** if the lead cannot write the check, the task
  does not go to Qwen.
- **Escalation (from the research):** after two failed attempts on the same
  task, the lead takes it back. A claim of "done" with no passing check counts
  as a failed attempt.

## Goals

1. One command the lead runs to spawn a Qwen worker on a task, in its own git
   worktree and branch, in a new tmux pane.
2. Worker panes are **full interactive sessions**. Wai can type into one and
   steer it.
3. Layout: the lead keeps the **left half** of the window. Workers split the
   right half, with each pane **as close to a phone (portrait) shape as
   possible**.
4. Every routing decision is logged and can be audited, including the
   Claude-side cost of each delegation.
5. Works in any git repo, with no setup per repo.

## Non-goals

- No automatic router model. The lead (Claude) triages.
- No changing how Claude-only work runs today.
- Not a replacement for spire-trio's peer-session setup.

## Anti-goals (would make this a failure even if it runs)

- **Delegation that costs Claude more than it saves.** The delegate-local
  project measured 2,159 delegations and shut down in October 2026 for this
  reason: Claude spent more tokens briefing Qwen and reading back its text
  than Qwen saved. So **Qwen must act as an agent** (edit files, run the check
  itself) and return a short report, never text that Claude then applies.
- **Parallel bad work.** Wai: "always prefer slower good work over parallel
  bad work." One worker with enough context beats two starved ones.
- **Silent fallbacks.** A worker failure must never show up as a Qwen success.
  In Akita's benchmark, a failed executor was quietly redone by the frontier
  model.
- **A worker touching anything outside its worktree**, Wai's working tree, or
  the remote.

## Constraints (measured 2026-11-10)

| Item | Value |
|---|---|
| GPUs Ollama sees | RTX 5070 Ti 14.7 GiB free + GTX 1660 5.0 GiB free = **19.7 GiB** |
| `qwen3.8:27b` (Ollama default Q4) | 18 GB, 256K context window, image input |
| Room left for context (KV cache) | ~1.7 GB, **unmeasured**. Qwen 3.5+ uses mostly linear attention, which may make long context cheap (unverified) |
| Ollama settings | 0.30.0, `OLLAMA_CONTEXT_LENGTH=8192`, `NUM_PARALLEL=2`, `MAX_LOADED_MODELS=1`, `KEEP_ALIVE` = forever, flash attention on |
| Claude Code | 2.1.322 |
| Host RAM in WSL | 39 GB |
| Current local default | `gpt-oss:20b`; Qwen takes over its slot (one model loaded at a time) |

**GPU priority, Wai's call: Qwen wins over TTS.** Hosaka's server currently
runs `ollama stop gpt-oss:20b` on start. It must instead refuse to start, with
a clear message, while a crew worker is running.

## How it works

![task flow](../../arch/crew-flow.svg)

1. The lead triages a task with the routing rule and writes a log entry
   (`route`, `reason`).
2. For Qwen, the lead runs `crew spawn` with a brief: goal, files, constraints,
   and **the check command**.
3. `crew` creates a worktree and branch off the lead's current HEAD, opens a
   pane, and starts a worker there: **Claude Code pointed at local Ollama**
   with Qwen as its model. Claude Code is the worker's harness, so Qwen gets
   the same file and shell tools; Ollama speaks the Anthropic API since
   January 2026. The brief is the worker's first message, and a crew-worker
   role is appended to its system prompt.
4. The worker edits, runs the check, and loops on its own until the check
   passes or it gives up. It then commits on its branch and reports back to
   the lead in one short message: pass/fail, the check's output tail, and a
   diff stat. The report goes over SendMessage, the same channel spire-trio
   uses, if a worker session pointed at Ollama can reach it (see Builder
   decides). It stays up so Wai can scroll back or steer it.
5. The lead reads the diff (a full read only when the stat or check output
   warrants it) and runs the check itself in the worktree. It then either
   merges and logs `accepted`, or sends it back with notes (attempt 2), or
   takes it over and logs `escalated`.
6. `crew close` removes the pane and the worktree after merging, and logs the
   outcome with the cost fields.

### Layout

![pane layout](../../arch/crew-layout.svg)

- The lead pane is pinned to 50% of the window width, on the left.
- Workers tile the right half in a grid of `c` columns by `r` rows (`c·r ≥ N`).
- Terminal cells are about twice as tall as wide. A 9:19.5 portrait phone
  shape is therefore a pane with **columns ≈ 0.92 × rows**. `crew` picks the
  grid whose panes' `cols/rows` is closest to 0.92, compared in log space.
  Example: a 240×60 window gives a 120×60 right half. One worker fills it
  (ratio 2.0, the best one pane can do). Two workers sit **side by side** at
  60×60 (ratio 1.0), not stacked (120×30, ratio 4.0).
- The layout is recomputed whenever a worker is added or closed, and when the
  window resizes, if tmux makes that cheap (builder's call).
- `crew` refuses to run outside tmux.

### Concurrency (Claude's call, under Wai's "slower good work" rule)

- **Default is one worker at a time.** A second spawn queues and starts when
  the first closes.
- `crew` allows two concurrent workers only if a measured test shows both get
  a **usable context (≥ 64K tokens) at once without spilling to CPU**. Ollama
  shares one loaded model between parallel requests, and each slot needs its
  own KV cache.
- **Never offload to CPU.** Each worker gets the context that fits on the GPU.

### Context and compaction (Claude's call)

- The worker's Claude Code system prompt and tools alone take a large share of
  context (the builder measures how much). Ollama's 8192 default is far too
  small. The crew model gets its own context setting (a Modelfile variant,
  e.g. `qwen3.8-crew`). Target **64K**; the floor is whatever the measured
  test proves fits at one worker. Below 32K, use the smaller Q3/IQ4 quant on
  the 5070 Ti alone instead.
- Worker Claude Code must compact against the **real** local window, not the
  200K it assumes for an unknown model. The builder finds the setting
  (environment variable or model config) and verifies compaction fires before
  Ollama truncates.
- Briefs stay small: one task, named files, one check. Over-scoping is the
  main way local models fail.

### Isolation and safety

- Worktrees live in a crew directory per repo, on a branch `crew/<task-id>`.
  Workers start in their worktree.
- Workers run with no Anthropic credentials: `ANTHROPIC_API_KEY` unset,
  `ANTHROPIC_BASE_URL` pointed at local Ollama, and every model slot (main,
  small/fast, subagent) set to the Qwen model. **No worker request may reach
  Anthropic.** The test confirms this.
- Never `--dangerously-skip-permissions`. Workers run in a permission mode
  that auto-accepts edits inside the worktree and allows the brief's check
  command. Anything else prompts in the pane, where Wai can see and answer it.
  Denied for workers: `git push`, and edits outside the worktree.
- The walled emet setup on this machine is untouched. A worker in an emet
  checkout runs as the same user as the lead and hits the same wall.

## Audit log

One JSON line per task in `~/.claude/crew/log.jsonl`, plus `crew log` to print
a table, filterable by repo, route, and outcome.

| Field | Meaning |
|---|---|
| `ts`, `repo`, `task_id` | when, where, which |
| `summary` | one line, the lead's words |
| `route` | `qwen`, `claude-subagent`, or `claude-self` |
| `reason` | why that route, one line (routing-rule language) |
| `check` | the pass/fail command (Qwen routes) |
| `attempts`, `outcome` | `accepted`, `sent-back` (then the next attempt), `escalated`, `failed` |
| `answered_by` | the model that actually answered, read from the worker transcript, so silent fallbacks show |
| `worker_wall_s`, `worker_tokens` | Qwen's time and tokens |
| `lead_tokens` | Claude's tokens spent briefing and reviewing this task (best estimate the builder can get from the lead's transcript) |

Claude-only routes get a log line too, so audits can catch tasks that should
have gone to Qwen. The point of the log is the cost-per-accepted-change
question: did a delegation save Claude usage once briefing and review are
counted?

## Daily retrospective

Wai: "I want Claude to run retrospectives daily to see how it can utilize the
subagents better."

- **When:** once a day, from a local cron or systemd user timer, so it can
  read the local log (a cloud routine cannot). It runs headless `claude -p`
  with a retro prompt. It is skipped on days with no new log lines, so idle
  days cost nothing.
- **What it reads:** that day's `log.jsonl` lines, the worker reports, and
  the earlier retros.
- **What it looks for:**
  - Misroutes: Claude routes that had a checkable result, and Qwen routes
    that escalated.
  - Cost per accepted change: `lead_tokens` against the Qwen work done.
  - Briefs that led to send-backs, and what they were missing.
  - Repeated failure patterns: task types Qwen keeps failing.
  - Idle GPU time while Claude did checkable work.
- **What it writes:** `~/.claude/crew/retros/YYYY-MM-DD.md` with a short
  table of findings and up to three concrete changes.
- **What it may change on its own (Claude's call):** the skill's "routing
  lessons" list (worked examples: route X went to Qwen and passed; route Y
  should have stayed with Claude) and the brief template. These changes are
  committed in this repo with the retro's date. A change to the routing rule
  itself, to concurrency, or to the model is written up as a proposal in
  `OPEN-QUESTIONS.md` for Wai, never applied.

## How the lead knows to do this

- A global skill, `crew`, holds the routing rule, the brief template, the
  review steps, and the commands. A one-line pointer in the global
  `~/.claude/CLAUDE.md` tells every session the skill exists and when to use
  it.
- A crew-worker role prompt, appended to every worker's system prompt (as in
  spire-trio's `spire-roles/`): stay in the worktree, run the check, commit,
  report in the fixed short format, and stop.

## Pieces

| Piece | Where |
|---|---|
| `crew` script (spawn / list / close / log / layout) | this repo `crew/crew.sh`, symlinked as `~/bin/crew` |
| Worker role prompt + worker config | this repo `crew/worker.md`; installed config dir `~/.claude/crew/worker-config` |
| Lead skill | this repo `crew/skill/SKILL.md`, symlinked to `~/.claude/skills/crew` |
| Runtime state | `~/.claude/crew/` (`log.jsonl`, `queue`, `running/`) |
| Crew model variant | Ollama Modelfile on `qwen3.8:27b` with the chosen context |
| Hosaka change | hosaka repo: refuse to start while a crew worker is running, instead of stopping the LLM |
| Audit log | `~/.claude/crew/log.jsonl` |

## Decided by Wai

Max plan; aim is more work per usage window using local resources. Routing
rule as quoted above; Claude triages; every choice logged for audit. Global,
not per-project. Workers in git worktrees. Workers are full sessions in tmux
panes. Lead takes the left half; workers split the rest, each pane as close
to phone-shaped as possible. Qwen wins over TTS for the GPU. Claude decides
the context and compaction settings, with slower good work preferred over
parallel bad work.

## Claude's calls (Wai can overturn)

- No-check-means-no-Qwen corollary; escalation after two failed attempts.
- One worker at a time by default; a second only after the measured test.
- Context target 64K, floor 32K, smaller quant below that; never CPU offload.
- Worker harness is Claude Code against Ollama, not Aider.
- The phone-shape math above (0.92 cols/rows target).
- Log location and fields.

## Settled after the builder review (Claude's calls)

- **The check.** `crew spawn <brief-file> <check-command>`. `crew` writes the
  check command into `.crew/check.sh` in the worktree, which makes compound
  commands with pipes and `&&` fine. The worker's allowlist gets the single
  rule `bash .crew/check.sh`. The worker runs it while it works, and the lead
  reruns the same script before it accepts. The lead's rerun is the one that
  counts.
- **Async, with a file-based queue.** `crew spawn` returns at once with a
  task id. The worker writes `.crew/report.md` and then exits its loop; it
  also sends SendMessage if that works (the test checks). The lead waits with
  a Monitor until-loop on the report file and can keep working in the
  meantime. At the concurrency limit, `crew spawn` appends to
  `~/.claude/crew/queue`, and `crew close` starts the next job. No daemon.
- **Model.** `qwen3.8:27b` (18 GB) exists in the Ollama library (checked
  2026-11-10). The fallback is a community IQ4_XS/Q3 GGUF pulled from
  Hugging Face (~14–15 GB) that fits on the 5070 Ti alone.
- **The plan is gated on the measured test.** Phase 1 is the test, and the
  plan stops there and reports to Wai. Pass/fail is decided at that gate:
  - Tool calls fail, or Claude Code's own prompt leaves under 16K of working
    room: try a lighter harness at the same worktree, pane, and log
    interface (opencode, then Aider) before building on Claude Code.
  - Every harness fails: stop and report.
- **Hosaka (repo `~/src/hosaka`, Wai's own; editing it is in scope).** Each
  running worker writes a lock file at `~/.claude/crew/running/<task-id>`,
  and hosaka refuses to start while any exist. In the other direction,
  following "Qwen wins vs TTS", `crew spawn` stops a running hosaka server
  first and logs that it did.
- **Egress bar: zero model requests to Anthropic.** That is the usage-limit
  point, and it is enforced by config:
  - Workers get their own `CLAUDE_CONFIG_DIR` (`~/.claude/crew/worker-config`)
    with no plugins, hooks, MCP servers, or login.
  - That config's CLAUDE.md is the worker role. The repo's own CLAUDE.md
    still loads, which is wanted.
  - `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` is set to drop telemetry and
    update checks.
  - The test confirms this with an `ss`/log check during a worker run.
  - A separate config dir may cut the worker off from the SendMessage
    sockets, so the report file is the main channel and SendMessage is extra.
- **`lead_tokens` is a rough figure (within a factor of 2), not exact.** It
  sums the usage of the lead's turns that ran a `crew` command or read that
  task's diff or report, taken from the lead's transcript. A rough figure is
  enough because the audit asks whether a delegation saved Claude usage, and
  the Qwen side is usually many times larger.
- **Dirty tree and merging.**
  - The worktree branches from the lead's committed HEAD. `crew spawn` warns
    if the files named in the brief have uncommitted changes, so the lead
    commits first.
  - On accept, the lead rebases the worker branch onto its current branch
    and fast-forwards.
  - A conflict means the lead (Claude) resolves it or sends the task back.
  - `crew` never pushes; pushing stays the lead's normal decision.
- **Where the source lives.** In this repo under `crew/` (script, worker
  role, skill), symlinked into `~/.claude/crew`, `~/.claude/skills/crew`, and
  `~/bin/crew`. That follows the dotfiles rule: project tooling lives in its
  own repo and is symlinked in, the way `voice` points at hosaka. The pointer
  line goes in the tracked `~/.claude/CLAUDE.md` (dotfiles repo, committed
  and pushed there).
- **emet.** No exclusion. A worker runs as the same user as the lead, so the
  kernel wall applies to it too, and workers only get the check command plus
  edits inside their worktree.

## Builder decides

- Script language and internals. Any worktree or pane cleanup on crash.
- The exact Claude Code env vars and settings for the Ollama backend and for
  the compaction window, verified on 2.1.322.
- How `lead_tokens` is estimated.

## First step: a measured test, before building

1. `ollama pull qwen3.8:27b`: an 18 GB download.
2. Load it at 32K, 64K, and 128K context with one and with two parallel
   requests. Record VRAM on both GPUs, whether any layer lands on CPU, and
   tokens/s.
3. Run one headless Claude Code task against it (a real edit plus a test run)
   in a scratch repo. Confirm the tool calls work, that no request leaves the
   machine, and how many tokens Claude Code's own prompt takes.

The concurrency and context defaults above get set from these numbers.
