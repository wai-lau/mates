# Mates Phase 1 (measured tests + pilot) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build only what the spec's "First step" needs: measure Qwen3.8-27B
on this GPU pair, prove the worker sandbox holds, and run a 20-task pilot
that scores the three bars. Then stop and report to Wai.

**Architecture:**
- **Workers are untrusted.** Each Qwen worker is headless Claude Code run as
  Unix user `claude` inside `bwrap`, with no network except one forwarded
  socket to a private Ollama.
- **`mates` does every git step**, as `wai`, outside the sandbox.
- **A bare pilot driver, `pilot.py`, runs each task end to end:**
  1. red-on-base check
  2. worktree
  3. worker
  4. neutral commit
  5. guard flags
  6. fresh blind verifier
  7. authoritative rerun
  8. Claude lead review
  9. log line

  It also runs the solo-Claude baseline. No tmux layout, no queue, no retro;
  those are Phase 2, built only if the pilot passes all three bars.

**Tech Stack:** bash, Python 3.12 stdlib (tests via `uv run --no-project --with pytest pytest`), bubblewrap 0.9.0, git worktrees, Ollama 0.30.0, Claude Code 2.1.322.

**Spec:** `docs/superpowers/specs/2026-11-10-claude-qwen-mates-design.md`. Read
it first: "Threat model and sandbox", "Settled after the builder review",
"First step".

## Global Constraints

- Priorities, in order: **PII security, then output fidelity, then
  cost-saving.** The higher one wins every conflict.
- Every worker, verifier, and check run (the authoritative rerun included)
  runs **as user `claude` inside `bwrap`**. No fallback: if the sandbox
  cannot start, fail.
- Never `--dangerously-skip-permissions`.
- Worker sandbox:
  - writable: only its worktree plus its own state dir
  - not mounted: `/home/wai` and `/home/claude` (the sandbox gets an empty
    `/home`)
  - network: none (`--unshare-all`) except the forwarded Ollama socket
  - environment: empty (`--clearenv`) plus an allowlist
  - no Anthropic token anywhere inside
- The check script's original lives in
  `~/.claude/mates/running/<task-id>/check.sh` (mode 600). The worker sees a
  read-only copy at `.mates/check.sh`. The log records its sha256.
- A check that passes on the untouched base refuses to spawn unless
  `--green-base "<reason>"` is given.
- The worker's commit uses the neutral author `mates <mates@localhost>` with
  the message `proposed change <task-id>`, and no trailers.
- The verifier sees only: goal, diff (authorship terms redacted), the check.
  It never sees the worker's report or transcript. Its brief calls the work
  "a proposed change".
- The worker's Claude Code is pinned to 2.1.322 and runs with
  `DISABLE_AUTOUPDATER=1`.
- Model: `qwen3.8:27b` (18 GB). Context target 64K, floor 32K. Below 32K, the
  community IQ4_XS/Q3 GGUF on the 5070 Ti alone. **Never CPU offload.**
- Two failed attempts on a task means it escalates.
- The pilot's private data stays local. Task files, the log, transcripts and
  the report live in `~/.claude/mates/` and are **never committed**. This repo
  is pushed, so it only ever gets code, prompts, and tests with no private
  repo content.
- Pilot bars:
  - PII: step 4 fully passes, and zero outbound connections across all 20
    runs.
  - Fidelity: zero false passes.
  - Cost: Claude tokens per accepted change ≤ 50% of the solo baseline.
- Mermaid goes inline in docs; no pre-rendered SVGs (Wai, 2026-11-10).

## Review Focus

1. **Check needs a runtime missing in the sandbox.** For example, a repo
   `.venv`, `node_modules`, or a tool under `/home/wai/.local`. Expected: the
   red-on-base run reports `runtime-missing`, distinct from "check failed",
   and the task is dropped from the pilot, not run. Test in Task 7
   (`test_red_on_base_runtime_missing`).
2. **Worker never writes `.mates/report.md`.** It crashes, times out, or
   stops early. Expected: the attempt counts as failed with
   `outcome: failed`, and nothing is committed. Test in Task 7
   (`test_missing_report_is_failed_attempt`).
3. **Worker deletes or edits `.mates/check.sh`, or adds files under `.mates/`.**
   Expected: the read-only bind blocks the write. Anything else under
   `.mates/` raises the `mates-dir` flag and is never committed. Test in
   Task 5 (`test_flag_mates_dir`) and Task 3's escape script.
4. **Empty diff with a passing worker report.** Expected: outcome `failed`
   ("no change"), never `accepted`. Test in Task 7
   (`test_empty_diff_not_accepted`).
5. **Ollama answers with a different model than requested,** or the request
   errors. Expected: the forwarder logs both names, `answered_by` comes from
   the response, and a mismatch fails the attempt. Test in Task 2
   (`test_records_response_model`) and Task 7
   (`test_model_mismatch_fails`).

---

## File map

| File | Responsibility |
|---|---|
| `mates/ctx.py` | Context measurement driver and `pick_ctx` decision |
| `mates/fwd.py` | Two-sided Ollama socket forwarder plus per-request usage log |
| `mates/sandbox.sh` | Builds the `bwrap` command; runs it as `claude` via `sudo -n -u claude` |
| `mates/worker-env.sh` | Env allowlist and Claude Code settings for workers (sourced inside sandbox.sh) |
| `mates/worker-settings.json` | Worker permission rules (second layer) |
| `mates/prompts/worker.md`, `verifier.md`, `lead-review.md` | Role prompts |
| `mates/guard.py` | Brief loading, guard flags, authorship redaction |
| `mates/logrec.py` | Audit log record and JSONL append |
| `mates/pilot.py` | One-task pipeline, solo baseline, `run-all`, `report` |
| `tests/` | pytest tests per module; `tests/sandbox_escape.sh` |

Runtime layout:
- **`/srv/mates/`** (wai:emetcode, mode 2770, created once by Wai)
  - `bin/claude-2.1.322`
  - `tasks/<id>/{wt,verify-wt,rerun-wt,ctl,state,sock}`
- **`~/.claude/mates/`** (wai only)
  - `log.jsonl`, `fwd/<id>.jsonl`
  - `running/<id>/check.sh`
  - `pilot/{tasks,results,report.md}`

---

### Task 1: Pull the model and measure context

**Files:**
- Create: `mates/ctx.py`
- Test: `tests/test_ctx.py`

**Interfaces:**
- Produces:
  - `pick_ctx(rows: list[dict]) -> dict`. Each row is
    `{"ctx": int, "parallel": int, "cpu_layers": int, "vram_mib": [int, int], "tok_s": float}`.
    It returns `{"num_ctx": int|None, "parallel": 1|2, "fallback_quant": bool}`.
  - `~/.claude/mates/ctx.json`, holding the chosen values (read by Task 4
    and Task 7).

- [ ] **Step 1: Write failing tests for `pick_ctx`**

```python
def test_picks_largest_gpu_only_ctx_at_one_worker():
    rows=[r(32768,1,0),r(65536,1,0),r(131072,1,3)]
    assert pick_ctx(rows)=={"num_ctx":65536,"parallel":1,"fallback_quant":False}
def test_two_workers_only_if_both_get_64k_gpu_only():
    rows=[r(65536,1,0),r(65536,2,0)]
    assert pick_ctx(rows)["parallel"]==2
    rows=[r(65536,1,0),r(65536,2,2)]
    assert pick_ctx(rows)["parallel"]==1
def test_below_32k_means_fallback_quant():
    assert pick_ctx([r(32768,1,4)])=={"num_ctx":None,"parallel":1,"fallback_quant":True}
```

`r(ctx, parallel, cpu_layers)` is a test helper. "GPU-only" means
`cpu_layers == 0`. For `parallel == 2`, `ctx` is per slot.

- [ ] **Step 2: Run, expect FAIL** (`uv run --no-project --with pytest pytest tests/test_ctx.py -v`: ImportError).
- [ ] **Step 3: Implement `pick_ctx` plus a `measure` CLI in `mates/ctx.py`**

  `python3 mates/ctx.py measure` does the following:
  1. Unloads the system server's model (`ollama stop gpt-oss:20b`).
  2. Starts a private `ollama serve` as wai on `127.0.0.1:11435` with
     `OLLAMA_MODELS=/usr/share/ollama/.ollama/models OLLAMA_NOPRUNE=1 OLLAMA_FLASH_ATTENTION=1 OLLAMA_KEEP_ALIVE=10m OLLAMA_NUM_PARALLEL=<p>`
     (wai is in group `ollama`, so the models dir is readable).
  3. For each `(ctx, p)` in `{32K, 64K, 128K} × {1, 2}`:
     - sends `p` concurrent `/api/generate` calls with `options.num_ctx=ctx`
       and a prompt filling ~90% of `ctx`;
     - records `ollama ps` CPU/GPU split (parsed into `cpu_layers` from the
       server log's `offloaded N/M layers`), `nvidia-smi --query-gpu=memory.used`
       for both GPUs, and `eval_count/eval_duration`.
  4. Writes `~/.claude/mates/ctx-rows.json` and `ctx.json`.

  The private server is the pilot's only Ollama (Tasks 2–9). The system
  service stays untouched, so no root is needed.

- [ ] **Step 4: Run tests, expect PASS.**
- [ ] **Step 5: Pull and measure**

  1. Run `ollama pull qwen3.8:27b`. It's an 18 GB download through the
     system server (~10 min at the measured line rate; time it).
  2. Run `python3 mates/ctx.py measure`. Expected: `ctx.json` written, and
     the table is printed.
  3. Create the variant from `num_ctx`: write
     `FROM qwen3.8:27b\nPARAMETER num_ctx <n>` to
     `~/.claude/mates/Modelfile`, then run
     `ollama create qwen3.8-mates -f ~/.claude/mates/Modelfile`.
  4. If `fallback_quant` is true, stop and report to Wai. The fallback quant
     is a separate pull decision.

- [ ] **Step 6: Commit** `mates/ctx.py tests/test_ctx.py` with "feat(mates): context measurement and pick".

### Task 2: Ollama forwarder with usage log

**Files:**
- Create: `mates/fwd.py`
- Test: `tests/test_fwd.py`

**Interfaces:**
- Produces:
  - CLI `python3 mates/fwd.py out --sock <path> --upstream 127.0.0.1:11435 --log <jsonl> --model <name>`
    runs outside, as wai. It listens on a unix socket, creating it with mode
    660 and group emetcode.
  - CLI `python3 mates/fwd.py in --sock <path> --listen 127.0.0.1:11434`
    runs inside the sandbox.
  - `parse_usage(sse_or_json: bytes) -> dict` returns
    `{"response_model": str|None, "input_tokens": int, "output_tokens": int}`.
  - The `out` side writes one JSONL line per HTTP request:
    `{"ts","path","requested_model","response_model","input_tokens","output_tokens","status"}`.
  - `out` rejects (HTTP 403, logged) any request whose path is not
    `/v1/messages` or `/v1/messages/count_tokens`, or whose `model` is not
    `--model`.

- [ ] **Step 1: Write failing tests**
  - `test_parse_usage_streaming`: Anthropic SSE with `message_start`
    (model `qwen3.8-mates`, `input_tokens` 1200) and `message_delta`
    (`output_tokens` 340). Expect
    `{"response_model":"qwen3.8-mates","input_tokens":1200,"output_tokens":340}`.
  - `test_parse_usage_json`: the same from a non-stream JSON body.
  - `test_records_response_model`: a fake upstream (a threaded
    `http.server` on an ephemeral port) answers with model `other`. Run
    `out` and `in` in threads and POST through `in`. The log line has
    `requested_model == "qwen3.8-mates"` and `response_model == "other"`.
  - `test_rejects_wrong_path_and_model`: `GET /api/tags` and a POST with
    model `x` both get 403, and each has a log line with `status: 403`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement with `asyncio`.**
  - `out` must parse the HTTP request line, headers, and body; it forwards
    only allowed requests.
  - It tees the response bytes into `parse_usage` while streaming them back
    unmodified.
  - `in` is a byte pipe from TCP to the unix socket.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** "feat(mates): sandbox Ollama forwarder with usage log".

### Task 3: Sandbox and escape test (spec First step 4)

**Files:**
- Create: `mates/sandbox.sh`, `tests/sandbox_escape.sh`

**Interfaces:**
- Consumes: `fwd.py in` (Task 2).
- Produces:
  - `mates/sandbox.sh --task-dir <dir> [--no-bwrap-for-test] -- <cmd...>`.
    It runs `<cmd>` as `claude` in `<dir>/wt` (mounted at `/work`), and
    exits with `<cmd>`'s status.
  - Mounts:
    - **read-only:** `/usr`, the `/bin`, `/lib`, `/lib64`, `/sbin` symlinks,
      `/etc/{passwd,group,ssl,ca-certificates,alternatives,localtime}`,
      `/srv/mates/bin`, `<dir>/ctl/check.sh` at `/work/.mates/check.sh`,
      and `<dir>/ctl/{settings.json,CLAUDE.md}` at fixed paths inside the
      config dir;
    - **writable:** `/work`, and `<dir>/state` at `/home/mates/.claude-cfg`;
    - **sockets:** `<dir>/sock` at `/run/mates`;
    - **fresh:** `--tmpfs /home --dir /home/mates --tmpfs /tmp --proc /proc --dev /dev`.
  - Flags: `--unshare-all --die-with-parent --new-session --clearenv`, then
    `--setenv` for `PATH=/usr/bin:/bin`, `HOME=/home/mates`, `TERM`, plus
    what `worker-env.sh` (Task 4) adds.
  - Inside, before `<cmd>`, it starts `fwd.py in --sock /run/mates/ollama.sock`
    in the background (fwd.py is read-only bound at `/srv/mates/bin/fwd.py`).
  - `<dir>` must be under `/srv/mates/tasks/`; anything else is refused.
  - `--no-bwrap-for-test` exists only for the escape script's second run. It
    keeps the user switch and drops bwrap.

- [ ] **Step 1: One-time setup that Wai runs (root).** Ask Wai to type:
  `! sudo install -d -o wai -g emetcode -m 2770 /srv/mates /srv/mates/bin /srv/mates/tasks`.
  Then, as wai, copy `~/.local/share/claude/versions/2.1.322` to
  `/srv/mates/bin/claude-2.1.322` (mode 755) and install `mates/fwd.py`
  there (mode 644). Verify: `sudo -n -u claude /srv/mates/bin/claude-2.1.322 --version`
  prints `2.1.322`.

- [ ] **Step 2: Write `tests/sandbox_escape.sh`, the failing test.**
  1. Creates `/srv/mates/tasks/escape-test`, with a git worktree of this
     repo in `wt/`.
  2. Writes a canary `~/.mates-canary` (mode 600).
  3. Runs a probe script through `sandbox.sh` twice. The probe prints
     `PASS`/`FAIL` per line for each item below.
  4. Exits non-zero if any expectation is violated.

  | Probe | bwrap run expects | no-bwrap run expects |
  |---|---|---|
  | `whoami` = `claude` | yes | yes |
  | `env` has no `TOKEN`, `KEY`, `SECRET`, `ANTHROPIC_API_KEY` | none | none |
  | read `/home/wai/.mates-canary` | denied | denied |
  | read `/home/wai/.claude/.credentials.json` | denied | denied |
  | read `/home/claude/.claude/.credentials.json` | denied (no such path) | *succeeds or absent: recorded, not failed* |
  | `ls /home/wai/src` | denied | denied |
  | read `/home/wai/src/emet/data` | denied | denied |
  | TCP connect `1.1.1.1:443` (python socket, 3 s timeout) | fails | *recorded, not failed* |
  | resolve `example.com` | fails | *recorded, not failed* |
  | write into the repo's `.git` (path from `wt/.git`) | denied | denied |
  | write `/work/.mates/check.sh` | denied (read-only) | n/a in no-bwrap run (skip) |
  | POST to `127.0.0.1:11434/v1/messages` with the allowed model | reaches the forwarder (any HTTP status but connection-refused) | n/a |

- [ ] **Step 3: Run it, expect FAIL** (no `sandbox.sh`).
- [ ] **Step 4: Implement `mates/sandbox.sh`** per the Interfaces. Use
  `sudo -n -u claude /usr/bin/bwrap ...`, with arguments built in a bash
  array; no `eval`.
- [ ] **Step 5: Run `bash tests/sandbox_escape.sh`.** Expect every bwrap
  line PASS, the no-bwrap file and git lines PASS, and exit 0. Save the
  output to `~/.claude/mates/pilot/escape.txt` as PII-bar evidence.
- [ ] **Step 6: Commit** `mates/sandbox.sh tests/sandbox_escape.sh` with "feat(mates): bwrap sandbox as claude user + escape test".

### Task 4: Worker harness probe (spec First step 3) and gate

**Files:**
- Create: `mates/worker-env.sh`, `mates/worker-settings.json`, `mates/prompts/worker.md`

**Interfaces:**
- Consumes: `sandbox.sh` (Task 3), `fwd.py out` (Task 2), `ctx.json` (Task 1).
- Produces:
  - **`worker-env.sh`** adds `--setenv` pairs:
    - `ANTHROPIC_BASE_URL=http://127.0.0.1:11434`, `ANTHROPIC_AUTH_TOKEN=ollama`
    - `ANTHROPIC_MODEL` and all three `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL` set to `qwen3.8-mates`
    - `CLAUDE_CODE_SUBAGENT_MODEL=qwen3.8-mates`
    - `CLAUDE_CONFIG_DIR=/home/mates/.claude-cfg`
    - `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_AUTOUPDATER=1`
    - the compaction-window variable this task identifies, set to `num_ctx`
  - **`worker-settings.json`** has permissions:
    - allow: `Read`, `Edit`, `Write`, `Glob`, `Grep`, `Bash(bash .mates/check.sh)`
    - deny: `WebFetch`, `WebSearch`, `Bash(git push:*)`, `Bash(curl:*)`, `Bash(wget:*)`
    - no hooks, MCP, or plugins
  - **The worker command:**
    `/srv/mates/bin/claude-2.1.322 -p --output-format json --permission-mode acceptEdits --append-system-prompt "$(cat ctl/worker.md)" "<brief text>"`

- [ ] **Step 1: Find the compaction-window setting.**
  1. Run `strings /srv/mates/bin/claude-2.1.322 | grep -E 'AUTO_COMPACT|COMPACT_WINDOW|CONTEXT_WINDOW'`
     and check the Claude Code settings docs for that version.
  2. Record the variable name, and whether it takes tokens or a percentage,
     in `worker-env.sh` with a comment citing the source.
- [ ] **Step 2: Write `worker.md`.**
  - The worker works only in `/work`.
  - It runs `bash .mates/check.sh` to test.
  - It never edits `.mates/` except creating `.mates/report.md`.
  - It stops after writing the report.
  - The report format is exact:
    `STATUS: pass|fail` / `CHECK-TAIL:` (last 20 lines) / `FILES:` (one path per line).
  - It has no git, and must not try to commit.
- [ ] **Step 3: Probe run.**
  1. Build a scratch task dir with a tiny git repo: one Python function with
     a bug, plus a check `python3 -m unittest -q` that fails on base.
  2. Start `fwd.py out`.
  3. Run the worker through `sandbox.sh`.
  4. Record: exit status; whether the edit landed and the check passes; the
     forwarder's first request `input_tokens` (Claude Code's own prompt
     size); total tokens; wall time.
  5. While it runs, in another shell, run `ss -tnp` for uid 1001 and confirm
     no non-loopback sockets.
- [ ] **Step 4: Compaction check.**
  1. Rerun with a brief that makes the worker read files until it passes
     `num_ctx`.
  2. Confirm from the transcript in `state/` that compaction fired before
     the forwarder saw any request with `input_tokens > num_ctx`.
- [ ] **Step 5: Gate (spec).** Working room = `num_ctx` − the first
  request's `input_tokens`.
  - If tool calls failed, or working room < 16384: stop and report to Wai,
    with opencode, then Aider, as the next harness to try.
  - Otherwise write the numbers to `~/.claude/mates/pilot/probe.json`.
- [ ] **Step 6: Commit** the three files with "feat(mates): worker env, settings, role prompt".

### Task 5: Guard flags, brief, and redaction

**Files:**
- Create: `mates/guard.py`
- Test: `tests/test_guard.py`

**Interfaces:**
- Produces:
  - `load_brief(path) -> dict`. The brief is JSON
    `{"goal": str, "files": [str], "constraints": str}`. A missing key, or
    an empty `goal` or `files`, raises `ValueError`.
  - `guard_flags(diff_text: str, brief_files: list[str]) -> list[str]`, with
    flags from:
    - `unnamed-file:<path>`
    - `test-file:<path>`
    - `test-skipped:<path>`
    - `assertion-removed:<path>`
    - `mates-dir:<path>`
    - `authorship:<path>`
  - `redact_authorship(diff_text: str) -> str` replaces matches with
    `[redacted]`.
- Rules:
  - A test file matches `(^|/)(tests?|spec|__tests__)/|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$`.
  - Skipped means an added line matching
    `pytest\.mark\.(skip|xfail)|unittest\.skip|\bxit\(|\.only\(|\bt\.Skip\(|\bskip\(`.
  - Assertion removed means a removed line matching
    `\bassert|expect\(|\.should\b|assertEqual|assertTrue`.
  - Authorship means an added line matching, case-insensitive,
    `\b(qwen|claude|anthropic|gpt|llm|language model|ai[- ]generated|as an ai)\b`.

- [ ] **Step 1: Write failing tests**, one per flag:
  - `test_flag_unnamed_file`
  - `test_flag_test_file`
  - `test_flag_skip_added`
  - `test_flag_assertion_removed`
  - `test_flag_mates_dir`
  - `test_flag_authorship`
  - `test_clean_diff_no_flags` (only brief files, no tests touched → `[]`)
  - `test_redact_keeps_rest` (`+# written by Qwen` → `+# written by [redacted]`)
  - `test_load_brief_rejects_empty_files`

  Each uses a small inline unified diff.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** Parse files from `+++ b/` headers; use per-file
  hunks for added and removed lines.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** "feat(mates): guard flags, brief, authorship redaction".

### Task 6: Audit log record

**Files:**
- Create: `mates/logrec.py`
- Test: `tests/test_logrec.py`

**Interfaces:**
- Produces:
  - `FIELDS`: the spec's audit fields, exactly:
    `ts, repo, task_id, summary, route, reason, check, check_sha, red_on_base, flags, attempts, outcome, answered_by, worker_wall_s, worker_tokens, verdict, lead_tokens`,
    plus pilot-only `arm` (`mates`|`solo`) and `full_diff_read` (bool).
  - `append(path, rec: dict) -> None` validates the record and writes one
    line.
- Rules:
  - `route` ∈ {`qwen`, `claude-subagent`, `claude-self`}.
  - `outcome` ∈ {`accepted`, `sent-back`, `escalated`, `failed`}.
  - An unknown key, or a missing required key, raises `ValueError`.
  - `ts` is added if absent (ISO-8601 UTC).
  - Writes use `O_APPEND` and one `write()` call per line.

- [ ] **Step 1: Write failing tests:**
  - `test_append_roundtrip`
  - `test_rejects_unknown_route`
  - `test_rejects_unknown_field`
  - `test_ts_added`
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** "feat(mates): audit log record".

### Task 7: One-task pipeline (`pilot.py run-one`)

**Files:**
- Create: `mates/pilot.py`, `mates/prompts/verifier.md`, `mates/prompts/lead-review.md`
- Test: `tests/test_pilot.py`

**Interfaces:**
- Consumes:
  - `sandbox.sh` (Task 3)
  - `fwd.py` (Task 2)
  - `worker-env.sh` and `worker-settings.json` (Task 4)
  - `guard.load_brief`, `guard_flags`, `redact_authorship` (Task 5)
  - `logrec.append` (Task 6)
- Produces:
  - `run_one(task: dict, *, claude_bin: str, lead_cmd: list[str]) -> dict`.
    It returns the log record it appended.
  - A task is `{"id","repo","base","brief","check","green_base_reason"?}`,
    where `brief` is a brief dict.
  - The env var `MATES_SANDBOX` overrides the sandbox command, so tests can
    use a stub (the same pattern as spire-trio's `SPIRE_CLAUDE`).
- Pipeline. Each step is a function; a test stubs subprocesses through the
  env vars.
  1. **`prepare`**:
     - `umask 002`;
     - `git -C repo worktree add --detach /srv/mates/tasks/<id>/wt <base>`;
     - write `ctl/check.sh` (mode 644) and the original to
       `~/.claude/mates/running/<id>/check.sh` (mode 600);
     - write `ctl/settings.json` and `ctl/CLAUDE.md` (= `worker.md`);
     - `mkdir .mates` in `wt`.
  2. **`red_on_base`** runs the check through the sandbox.
     - Exit 0 without `green_base_reason`: refuse
       (`outcome: failed`, `reason: green-base`).
     - Exit 126/127, or stderr matching
       `command not found|No such file|ModuleNotFoundError`: `runtime-missing`.
  3. **`run_worker`** starts `fwd.py out` with the log at
     `~/.claude/mates/fwd/<id>.jsonl` and runs the worker through
     `sandbox.sh` with the brief text. Timeout 45 min.
  4. **`collect`**:
     - read `.mates/report.md`; missing means a failed attempt;
     - `git -C wt add -A -- . ':!.mates'`;
     - an empty diff means failed, with reason "no change";
     - commit with `-c user.name=mates -c user.email=mates@localhost commit -m "proposed change <id>"`;
     - compute `guard_flags(git diff base..HEAD, brief.files)`.
  5. **`verify`**:
     - make a fresh worktree `verify-wt` at the commit;
     - put `goal.md` and `change.diff` (= `redact_authorship(diff)`) in its
       `ctl`, read-only bound at `.mates/goal.md` and `.mates/change.diff`;
     - run the verifier prompt through the sandbox, as a new process with a
       new `state/`;
     - read `.mates/verdict.md` (`VERDICT: pass|fail` + ≤5 `- ` findings).
  6. **`rerun`**: a fresh `rerun-wt` at the commit, then the sandbox runs the
     original `check.sh` (copied from `running/`, not from `ctl`). Record the
     tree sha that passed.
  7. **`lead_review`** runs `lead_cmd` (default:
     `env -u ANTHROPIC_API_KEY claude -p --output-format json --permission-mode default --tools Read`)
     with `lead-review.md` plus the fixed fields: verdict, diff stat, flags,
     rerun result, and the last 20 check lines.
     - It may request the full diff (`NEED-DIFF`), which triggers a second
       call with the diff. `full_diff_read` is set.
     - It answers `ACCEPT` or `SEND-BACK: <notes>`.
     - `lead_tokens` is the sum of `usage.input_tokens + output_tokens + cache_creation_input_tokens`
       from the JSON output, plus `cache_read_input_tokens`, recorded
       separately as `lead_cache_read`.
  8. **Loop.** A send-back, or a verifier/rerun fail, re-runs the worker in
     the same `wt` with the notes appended (attempt 2). A second fail means
     `escalated`.
  9. **`answered_by`** is the set of `response_model` values in the
     forwarder log. Anything other than `{"qwen3.8-mates"}` fails the
     attempt.
  10. **Log** with `route: qwen`, `arm: mates`.
- Fixed copy:
  - `verifier.md`: "You are reviewing a proposed change. `.mates/goal.md`
    says what it should do; `.mates/change.diff` is the change, already
    applied in `/work`. Run `bash .mates/check.sh`. Then write
    `.mates/verdict.md`: first line `VERDICT: pass` or `VERDICT: fail`, then
    at most 5 lines starting `- `. Pass only if the check passes and the
    change does what the goal says without unrelated edits."
  - `lead-review.md`: "Decide whether to merge this proposed change. Reply
    with exactly one line: `ACCEPT`, `SEND-BACK: <one-line notes>`, or
    `NEED-DIFF`. Ask for the diff when any flag is set, the verdict is fail,
    or the diff stat touches files outside the goal."

- [ ] **Step 1: Write failing tests**. They use a stub sandbox (a shell
  script that runs the command without bwrap, in a temp git repo), a stub
  worker (it writes a fixed file plus a report), and a stub `lead_cmd`
  (prints fixed JSON).
  - `test_green_base_refused`
  - `test_red_on_base_runtime_missing`
  - `test_missing_report_is_failed_attempt`
  - `test_empty_diff_not_accepted`
  - `test_commit_is_neutral` (author `mates <mates@localhost>`, message
    `proposed change t1`, no trailers)
  - `test_verifier_sees_no_report` (the verifier stub asserts that
    `.mates/report.md` is absent in `verify-wt`)
  - `test_rerun_uses_original_check` (the `ctl` copy is modified after
    collect; the rerun still uses the original)
  - `test_send_back_then_escalate` (lead stub sends back twice →
    `outcome: escalated`, `attempts: 2`)
  - `test_model_mismatch_fails` (fake forwarder log with `response_model`
    `gpt-oss:20b`)
  - `test_accept_logs_record` (all fields present; `logrec` validates)
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement `mates/pilot.py`** per the pipeline, plus the
  `run-one --task <json>` CLI.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Real smoke run.** Run `python3 mates/pilot.py run-one --task`
  on the Task 4 scratch repo task, with the real sandbox and the real lead.
  Expected: a log record with `outcome: accepted`, `answered_by` =
  `["qwen3.8-mates"]`, and the forwarder log showing only allowed requests.
- [ ] **Step 6: Commit** "feat(mates): one-task pilot pipeline".

### Task 8: Pilot task set and solo baseline

**Files:**
- Modify: `mates/pilot.py` (add `build-tasks`, `run-solo`)
- Test: `tests/test_pilot.py` (add)

**Interfaces:**
- Produces:
  - `~/.claude/mates/pilot/tasks/<id>.json`, 20 files in the Task 7 task
    schema plus `target` (the historical commit).
  - `run_solo(task, *, claude_cmd) -> dict` logs `arm: solo`,
    `route: claude-subagent`, and `lead_tokens` (the solo session's total
    tokens, the same formula as Task 7).

- [ ] **Step 1: Write failing tests:**
  - `test_task_valid_requires_red_base_green_target`: stub check exits 1 on
    base and 0 on target → valid; 0/0 → invalid.
  - `test_solo_logs_arm_solo`
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement `build-tasks`.**
  - **Candidates:** non-merge commits from the last 6 months in Wai's
    repos under `~/src` (excluding `emet`, whose data is walled and out of
    pilot scope), that change 1–3 non-test source files and also add or
    change a test.
  - **Each task:**
    - `base` = the parent commit; `target` = the commit;
    - the check is the repo's test command, narrowed to the changed test
      files;
    - the brief comes from the commit message and the touched files, written
      by a Claude subagent with the same prompt for every task;
    - test files go in `brief.files` only when the commit itself is test
      work.
  - **Keep** the task only if both runs succeed in the sandbox: the check
    is red on `base` and green on `target`, with no `runtime-missing`.
  - Stop at 20.
  - The briefs cost the same in both arms and are excluded from both
    totals.
- [ ] **Step 4: Implement `run-solo`.**
  - It runs as wai in a fresh worktree:
    `env -u ANTHROPIC_API_KEY claude -p --output-format json --permission-mode acceptEdits --allowedTools "Bash(bash .mates/check.sh)" "<brief text>"`.
  - The check comes from the same `check.sh`.
  - It is normal Claude use on Wai's own repos, so it uses no sandbox and no
    Qwen.
- [ ] **Step 5: Run, expect PASS.** Then run `python3 mates/pilot.py build-tasks`.
  Expected: 20 task files. If fewer than 20 qualify, stop and report the
  count to Wai.
- [ ] **Step 6: Commit** code only: "feat(mates): pilot task builder + solo baseline". Task files stay in `~/.claude/mates/pilot/`.

### Task 9: Run the pilot, score the bars, report

**Files:**
- Modify: `mates/pilot.py` (add `run-all`, `score`)
- Test: `tests/test_pilot.py` (add)

**Interfaces:**
- Produces:
  - `score(records: list[dict], fidelity: dict[str, bool], escape_ok: bool, outbound: int) -> dict`.
    It returns `{"pii": bool, "fidelity": bool, "cost": bool, "mates_tokens_per_accept": float, "solo_tokens_per_accept": float, "ratio": float}`.
  - `~/.claude/mates/pilot/report.md`.

- [ ] **Step 1: Write failing tests for `score`:**
  - `test_cost_bar_at_50_percent` (ratio 0.50 passes, 0.51 fails)
  - `test_any_false_pass_fails_fidelity`
  - `test_outbound_nonzero_fails_pii`
  - `test_no_accepts_fails_cost` (no accepted mates change → ratio `inf`)

  Tokens per accepted change = the arm's total `lead_tokens` (attempts and
  escalations included) divided by its accepted count.
- [ ] **Step 2: Run, expect FAIL.** **Step 3: Implement.** **Step 4: Run, expect PASS.**
- [ ] **Step 5: `run-all`.**
  1. Shuffle the 20 tasks with a fixed, logged seed.
  2. For each task: `run-one`, then `run-solo`, alternating which arm goes
     first.
  3. During the run, a background sampler logs
     `ss -tnp state established` for uid 1001 every 5 s to
     `~/.claude/mates/pilot/net.log`. `outbound` = the number of entries with
     a non-loopback peer.
  4. Wall-time ETA: measure the first two tasks, then report to Wai the
     projected total and the Claude usage spent so far.
- [ ] **Step 6: Fidelity read.** For every accepted change in either arm, a
  fresh Claude subagent compares the accepted diff with the `target` commit
  and the brief, and answers `CORRECT` or `WRONG: <reason>`. `WRONG` counts
  as a false pass. This is evaluation cost, outside both arms.
- [ ] **Step 7: `score`, then write `report.md`.**
  - A bar table (labelled columns: bar, pass rule, measured, result).
  - Per-arm accepts, escalations, tokens per accepted change, worker wall
    time.
  - The ctx and probe numbers.
  - No repo names in the summary section.
- [ ] **Step 8: Commit** code only: "feat(mates): pilot run-all + scoring".
- [ ] **Step 9: Gate.** Report to Wai: the bar table, plus the link to the
  local report (copy it to `~/review.md`). Stop here. Phase 2 (tmux layout,
  queue, `mates` launcher, hosaka lock, retro) is planned only after all
  three bars pass.
