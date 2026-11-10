#!/usr/bin/env python3
"""Bare mates pilot: one task end to end, the solo baseline, and scoring.

    pilot.py run-one --task <task.json>

Pipeline per task (spec "How it works" + "Settled after the builder review"):
red-on-base -> worktree -> worker (sandboxed Qwen) -> neutral commit + guard
flags -> fresh blind verifier -> authoritative rerun of the ORIGINAL check ->
Claude lead review on fixed fields -> log line. Two failed attempts escalate.
"""
import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import time

import fsutil
import guard
import logrec

HERE = pathlib.Path(__file__).resolve().parent
PROMPTS = HERE / "prompts"
MODEL = "mates-qwen"
TASKS_ROOT = pathlib.Path("/srv/mates/tasks")
STATE_ROOT = pathlib.Path.home() / ".claude" / "mates"
# Claude calls (lead review, fidelity) run from here: outside /home/wai, so no CLAUDE.md is discovered
# from parent dirs (~/CLAUDE.md, ~/.claude/CLAUDE.md cost ~7.6K tokens per call, measured).
NEUTRAL_CWD = pathlib.Path("/srv/mates/neutral")
CLAUDE_BIN = "/srv/mates/bin/claude-2.1.322"
# The lead review is one turn of a session that already exists, so the pilot runs it with a minimal
# system prompt (the review instructions) and no tools/MCP: what is counted is the review's own cost.
LEAD_CMD = ["env", "-u", "ANTHROPIC_API_KEY", "claude", "-p", "--output-format", "json", "--tools", "",
            "--strict-mcp-config", "--setting-sources", "project", "--system-prompt-file", str(PROMPTS / "lead-review.md")]
MISSING = re.compile(r"command not found|No module named|cannot execute")
MAX_ATTEMPTS = 2


def _git(repo, *a, check=True):
    return subprocess.run(["git", "-C", str(repo), *a], check=check, capture_output=True, text=True).stdout.strip()


def start_fwd(sock, log, model):
    p = subprocess.Popen(["python3", str(HERE / "fwd.py"), "out", "--sock", str(sock), "--upstream",
                          "127.0.0.1:11435", "--log", str(log), "--model", model],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if pathlib.Path(sock).exists():
            break
        time.sleep(0.1)
    return p


class Ctx:
    def __init__(self, task, claude_bin, lead_cmd, sandbox, tasks_root, state_root, start_fwd, num_ctx, timeout,
                 runtime=None, injects=()):
        self.task, self.claude_bin, self.lead_cmd, self.sandbox = task, claude_bin, lead_cmd, sandbox
        self.tasks_root, self.state = pathlib.Path(tasks_root), pathlib.Path(state_root)
        self.start_fwd, self.num_ctx, self.timeout = start_fwd, num_ctx, timeout
        self.runtime, self.injects = runtime, list(injects)
        self.id, self.repo = task["id"], task["repo"]
        self.fwd_log = self.state / "fwd" / f"{self.id}.jsonl"
        self.orig_check = self.state / "running" / self.id / "check.sh"
        self.dirs = []

    def env_args(self):
        out = []
        for line in (HERE / "worker-env.sh").read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out += ["--setenv", line.replace("@NUM_CTX@", str(self.num_ctx))]
        if self.runtime:
            out += ["--runtime", str(self.runtime)]
        for inj in self.injects:
            out += ["--inject", inj]
        return out


def make_task_dir(c: Ctx, name: str, commit: str, check_text: str, extra: dict | None = None) -> pathlib.Path:
    td = c.tasks_root / name
    if td.exists():
        _git(c.repo, "worktree", "remove", "--force", str(td / "wt"), check=False)
        fsutil.rmtree(td)
    for d in ("ctl", "state", "sock"):
        (td / d).mkdir(parents=True)
    for d in ("state", "sock"):  # the worker (user claude, same group) writes here
        (td / d).chmod(0o2770)
    _git(c.repo, "worktree", "add", "--detach", str(td / "wt"), commit)
    (td / "wt" / ".mates").mkdir(exist_ok=True)
    files = {"check.sh": check_text, "settings.json": (HERE / "worker-settings.json").read_text(),
             "CLAUDE.md": (PROMPTS / "worker.md").read_text(), **(extra or {})}
    for k, v in files.items():
        p = td / "ctl" / k
        p.write_text(v)
        p.chmod(0o644)
    c.dirs.append(td)
    return td


def sandbox_run(c: Ctx, td, cmd, timeout=None):
    full = [c.sandbox, "--task-dir", str(td), *c.env_args(), "--", *cmd]
    try:
        return subprocess.run(full, capture_output=True, text=True, timeout=timeout or c.timeout)
    except subprocess.TimeoutExpired as e:
        dec = lambda b: b.decode("utf-8", "replace") if isinstance(b, bytes) else (b or "")
        return subprocess.CompletedProcess(full, 124, dec(e.stdout), dec(e.stderr) + "\ntimeout")


def red_on_base(c: Ctx, td) -> str:
    r = sandbox_run(c, td, ["bash", ".mates/check.sh"], timeout=600)
    if r.returncode in (126, 127) or (r.returncode != 0 and MISSING.search(r.stderr + r.stdout)):
        tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
        return "runtime-missing: " + tail[0][:200]
    return "green" if r.returncode == 0 else "red"


def brief_text(brief: dict, notes: list[str]) -> str:
    t = (f"Goal: {brief['goal']}\nFiles: {', '.join(brief['files'])}\n"
         f"Constraints: {brief.get('constraints') or 'none'}\nCheck: bash .mates/check.sh\n")
    if notes:
        t += "\nThe previous attempt was not accepted. Notes:\n" + "\n".join(f"- {n}" for n in notes) + "\n"
    return t


def run_worker(c: Ctx, td, text: str) -> float:
    report = td / "wt" / ".mates" / "report.md"
    if report.exists():
        report.unlink()
    t0 = time.time()
    sandbox_run(c, td, [c.claude_bin, "-p", "--output-format", "json", "--permission-mode", "acceptEdits",
                        "--append-system-prompt", (PROMPTS / "worker.md").read_text(), text])
    return time.time() - t0


def collect(td, base: str, brief: dict, task_id: str):
    """Commit the worker's edits (never .mates/) with a neutral author. Returns (commit, diff, flags, why)."""
    wt = td / "wt"
    if not (wt / ".mates" / "report.md").exists():
        return None, "", [], "no report"
    _git(wt, "add", "-A", "--", ".", ":!.mates")
    if not _git(wt, "diff", "--cached", "--name-only"):
        return None, "", [], "no change"
    _git(wt, "-c", "user.name=mates", "-c", "user.email=mates@localhost", "-c", "commit.gpgsign=false",
         "commit", "-q", "--no-verify", "-m", f"proposed change {task_id}")
    commit = _git(wt, "rev-parse", "HEAD")
    diff = _git(wt, "diff", f"{base}..{commit}")
    return commit, diff, guard.guard_flags(diff, brief["files"]), None


def verify(c: Ctx, attempt: int, commit: str, goal: str, diff: str, check_text: str) -> dict:
    blind = guard.redact_authorship(diff)
    (c.state / "results" / f"{c.id}.v{attempt}.diff").write_text(blind)
    td = make_task_dir(c, f"{c.id}-v{attempt}", commit, check_text, {"goal.md": goal + "\n", "change.diff": blind})
    fwd = c.start_fwd(td / "sock" / "ollama.sock", c.fwd_log, MODEL)
    try:
        sandbox_run(c, td, [c.claude_bin, "-p", "--output-format", "json", "--permission-mode", "acceptEdits",
                            "--append-system-prompt", (PROMPTS / "verifier.md").read_text(),
                            "Review the proposed change described in .mates/goal.md and .mates/change.diff."])
    finally:
        _stop(fwd)
    v = td / "wt" / ".mates" / "verdict.md"
    text = v.read_text() if v.exists() else "VERDICT: fail\n- no verdict written\n"
    return {"pass": text.splitlines()[0].strip() == "VERDICT: pass",
            "findings": sum(1 for l in text.splitlines()[1:] if l.startswith("- ")), "text": text[:2000]}


def rerun(c: Ctx, attempt: int, commit: str) -> tuple[bool, str, str]:
    td = make_task_dir(c, f"{c.id}-r{attempt}", commit, c.orig_check.read_text())
    r = sandbox_run(c, td, ["bash", ".mates/check.sh"], timeout=600)
    tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-20:])
    return r.returncode == 0, _git(c.repo, "rev-parse", f"{commit}^{{tree}}"), tail


def _lead_call(c: Ctx, prompt: str):
    import claudejson
    NEUTRAL_CWD.mkdir(exist_ok=True)
    r = subprocess.run(c.lead_cmd, input=prompt, capture_output=True, text=True, timeout=600, cwd=str(NEUTRAL_CWD))
    d = claudejson.parse(r.stdout)
    return d["result"], d["tokens"], d["cache_read"]


def lead_review(c: Ctx, fields: dict, diff: str):
    head = "Proposed change, fixed fields:\n" + json.dumps(fields, indent=1)
    ans, tok, cache = _lead_call(c, head)
    full = False
    if ans.startswith("NEED-DIFF"):
        full = True
        ans, t2, c2 = _lead_call(c, head + "\n\nFull diff:\n" + diff[:60000] + "\n\nNow answer ACCEPT or SEND-BACK.")
        tok, cache = tok + t2, cache + c2
    return ans, tok, cache, full


def answered_by(log: pathlib.Path) -> list[str]:
    if not log.exists():
        return []
    models = {json.loads(l).get("response_model") for l in log.read_text().splitlines() if l.strip()}
    return sorted(m for m in models if m)


def _stop(p):
    if p is not None:
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()


def run_one(task: dict, *, claude_bin=CLAUDE_BIN, lead_cmd=LEAD_CMD, sandbox=str(HERE / "sandbox.sh"),
            tasks_root=TASKS_ROOT, state_root=STATE_ROOT, start_fwd=start_fwd, num_ctx=65536, timeout=3600,
            runtime=None, injects=()) -> dict:
    os.umask(0o002)
    c = Ctx(task, claude_bin, lead_cmd, sandbox, tasks_root, state_root, start_fwd, num_ctx, timeout, runtime, injects)
    for d in ("fwd", "results", f"running/{c.id}"):
        (c.state / d).mkdir(parents=True, exist_ok=True)
    check_text = "#!/usr/bin/env bash\n" + task["check"] + "\n"
    c.orig_check.write_text(check_text)
    c.orig_check.chmod(0o600)
    if c.fwd_log.exists():
        c.fwd_log.unlink()
    brief = task["brief"]
    rec = {"repo": pathlib.Path(c.repo).name, "task_id": c.id, "summary": brief["goal"][:200], "route": "qwen",
           "reason": "checkable", "check": task["check"], "check_sha": hashlib.sha256(check_text.encode()).hexdigest(),
           "attempts": 0, "flags": [], "worker_wall_s": 0.0, "worker_tokens": 0, "lead_tokens": 0,
           "lead_cache_read": 0, "full_diff_read": False, "arm": "mates"}
    try:
        td = make_task_dir(c, c.id, task["base"], check_text)
        base_state = red_on_base(c, td)
        rec["red_on_base"] = base_state == "red"
        if base_state.startswith("runtime-missing") or (base_state == "green" and not task.get("green_base_reason")):
            rec.update(outcome="failed", detail="green-base" if base_state == "green" else base_state)
            return _finish(c, rec)
        notes, fwd = [], c.start_fwd(td / "sock" / "ollama.sock", c.fwd_log, MODEL)
        try:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                rec["attempts"] = attempt
                rec["worker_wall_s"] += round(run_worker(c, td, brief_text(brief, notes)), 1)
                commit, diff, flags, why = collect(td, task["base"], brief, c.id)
                if why:
                    notes.append(why)
                    continue
                rec["flags"], rec["commit"] = flags, commit
                (c.state / "results" / f"{c.id}.diff").write_text(diff)
                verdict = verify(c, attempt, commit, brief["goal"], diff, check_text)
                rec["verdict"] = {"pass": verdict["pass"], "findings": verdict["findings"]}
                ok, tree, tail = rerun(c, attempt, commit)
                models = answered_by(c.fwd_log)
                rec["answered_by"] = models
                if models != [MODEL]:
                    notes.append(f"answered by {models}")
                    continue
                if not verdict["pass"]:
                    notes.append("reviewer: " + verdict["text"].replace("\n", " ")[:500])
                    continue
                if not ok:
                    notes.append("check failed on rerun: " + tail[-500:])
                    continue
                stat = _git(td / "wt", "diff", "--stat", f"{task['base']}..{commit}")
                ans, tok, cache, full = lead_review(c, {"goal": brief["goal"], "verdict": verdict["text"],
                                                        "diff_stat": stat, "flags": flags, "rerun": "pass",
                                                        "check_tail": tail}, diff)
                rec["lead_tokens"] += tok
                rec["lead_cache_read"] += cache
                rec["full_diff_read"] = rec["full_diff_read"] or full
                if ans.startswith("ACCEPT"):
                    rec.update(outcome="accepted", tree=tree)
                    return _finish(c, rec)
                notes.append(ans.removeprefix("SEND-BACK:").strip() or ans)
            rec.update(outcome="escalated", detail="; ".join(notes)[:500])
            return _finish(c, rec)
        finally:
            _stop(fwd)
    finally:
        _cleanup(c)


def _cleanup(c: Ctx):
    """Keep each sandbox's in-namespace socket samples, then drop the worktrees."""
    (c.state / "net").mkdir(parents=True, exist_ok=True)
    for td in reversed(c.dirs):
        fsutil.open_up(td)
        nl = td / "sock" / "net.log"
        if nl.exists():
            with open(c.state / "net" / f"{c.id}.log", "a") as f:
                f.write(nl.read_text())
        keep = c.state / "transcripts" / c.id / td.name  # worker's Claude Code state: local audit only
        if (td / "state").is_dir() and not keep.exists():
            shutil.copytree(td / "state", keep, symlinks=True, ignore_dangling_symlinks=True)
        _git(c.repo, "worktree", "remove", "--force", str(td / "wt"), check=False)
        fsutil.rmtree(td)


def _rmtree(path):
    fsutil.rmtree(path)


def _finish(c: Ctx, rec: dict) -> dict:
    if rec.get("arm") != "solo":
        rec.setdefault("answered_by", answered_by(c.fwd_log))
    rec = {k: v for k, v in rec.items() if v is not None}
    if rec.get("arm") != "solo" and c.fwd_log.exists():
        rec["worker_tokens"] = sum(json.loads(l).get("input_tokens", 0) + json.loads(l).get("output_tokens", 0)
                                   for l in c.fwd_log.read_text().splitlines() if l.strip())
    log = {k: v for k, v in rec.items() if k in logrec.FIELDS or k in logrec.PILOT_FIELDS}
    logrec.append(c.state / "log.jsonl", log)
    return rec


SOLO_CMD = ["env", "-u", "ANTHROPIC_API_KEY", "claude", "-p", "--output-format", "json", "--strict-mcp-config",
            "--setting-sources", "project",
            "--permission-mode", "acceptEdits", "--allowedTools", "Bash(bash .mates/check.sh)"]


def run_solo(task: dict, *, claude_cmd=SOLO_CMD, sandbox=str(HERE / "sandbox.sh"), tasks_root=TASKS_ROOT,
             state_root=STATE_ROOT, runtime=None, timeout=2700) -> dict:
    """Baseline arm: a fresh Claude session does the task from the same brief, as wai, unsandboxed
    (normal Claude use on Wai's own repo). The pass that counts is the same sandboxed rerun as the mates arm."""
    os.umask(0o002)
    c = Ctx(task, claude_cmd[0], [], sandbox, tasks_root, state_root, None, 0, timeout, runtime)
    check_text = "#!/usr/bin/env bash\n" + task["check"] + "\n"
    c.orig_check.parent.mkdir(parents=True, exist_ok=True)
    c.orig_check.write_text(check_text)
    c.orig_check.chmod(0o600)
    brief = task["brief"]
    rec = {"repo": pathlib.Path(c.repo).name, "task_id": c.id, "summary": brief["goal"][:200],
           "route": "claude-subagent", "reason": "pilot solo baseline", "check": task["check"],
           "check_sha": hashlib.sha256(check_text.encode()).hexdigest(), "attempts": 1, "arm": "solo"}
    env = dict(os.environ)
    if runtime:
        import runtime as rtmod
        env.update(dict(l.split("=", 1) for l in rtmod.runtime_env(runtime)))
    try:
        td = make_task_dir(c, f"{c.id}-solo", task["base"], check_text)
        shutil.copy(td / "ctl" / "check.sh", td / "wt" / ".mates" / "check.sh")
        t0 = time.time()
        import claudejson
        r = subprocess.run(claude_cmd, input=brief_text(brief, []), capture_output=True, text=True,
                           timeout=timeout, cwd=str(td / "wt"), env=env)
        rec["worker_wall_s"] = round(time.time() - t0, 1)
        d = claudejson.parse(r.stdout)
        rec["lead_tokens"], rec["lead_cache_read"] = d["tokens"], d["cache_read"]
        (td / "wt" / ".mates" / "report.md").write_text("solo\n")
        commit, diff, flags, why = collect(td, task["base"], brief, c.id)
        rec["flags"] = flags
        if why:
            rec.update(outcome="failed", detail=why)
            return _finish(c, rec)
        rec["commit"] = commit
        (c.state / "results").mkdir(parents=True, exist_ok=True)
        (c.state / "results" / f"{c.id}.solo.diff").write_text(diff)
        ok, tree, tail = rerun(c, 1, commit)
        rec.update(outcome="accepted" if ok else "failed", tree=tree, detail=None if ok else tail[-300:])
        return _finish(c, rec)
    finally:
        _cleanup(c)


def score(records: list[dict], fidelity: dict[str, bool], escape_ok: bool, outbound: int) -> dict:
    """The pilot's three bars. fidelity maps "<arm>:<task_id>" -> correct? for accepted changes."""
    def per_accept(arm):
        rs = [r for r in records if r.get("arm") == arm]
        acc = sum(1 for r in rs if r["outcome"] == "accepted")
        tot = sum(r.get("lead_tokens", 0) for r in rs)
        return tot / acc if acc else float("inf")
    m, s = per_accept("mates"), per_accept("solo")
    ratio = m / s if s not in (0, float("inf")) else float("inf")
    return {"pii": bool(escape_ok and outbound == 0), "fidelity": all(fidelity.values()),
            "cost": ratio <= 0.5, "mates_tokens_per_accept": m, "solo_tokens_per_accept": s, "ratio": ratio}


def plan_order(task_ids: list[str], seed: int) -> list[tuple[str, tuple[str, str]]]:
    """Shuffled task order (fixed seed, logged), alternating which arm runs first."""
    import random
    ids = list(task_ids)
    random.Random(seed).shuffle(ids)
    return [(t, ("mates", "solo") if i % 2 == 0 else ("solo", "mates")) for i, t in enumerate(ids)]


def count_outbound(net_log) -> int:
    """Lines of `ss -tn` output (sampled for uid 1001) whose peer is not loopback."""
    n = 0
    for line in pathlib.Path(net_log).read_text().splitlines():
        addrs = [x for x in line.split() if ":" in x and x.rsplit(":", 1)[-1].isdigit()]
        if len(addrs) < 2:
            continue
        peer = addrs[1]
        if not (peer.startswith("127.") or peer.startswith("[::1]") or peer.startswith("::1")):
            n += 1
    return n


def render_report(s: dict, arms: dict, extra: str = "") -> str:
    ok = lambda b: "PASS" if b else "FAIL"
    rows = [("PII security", "sandbox escape test passes; zero outbound connections", "see escape.txt / net.log", s["pii"]),
            ("Output fidelity", "zero false passes among accepted changes", "fidelity reads", s["fidelity"]),
            ("Cost-saving", "Claude tokens per accepted change <= 50% of solo",
             f"{s['mates_tokens_per_accept']:.0f} vs {s['solo_tokens_per_accept']:.0f} ({s['ratio']:.0%})", s["cost"])]
    md = "# Mates pilot report\n\n| Bar | Pass rule | Measured | Result |\n|---|---|---|---|\n"
    md += "".join(f"| {a} | {b} | {c} | {ok(d)} |\n" for a, b, c, d in rows)
    md += "\n| Arm | Accepted | Escalated | Failed | Worker wall time (s) |\n|---|---|---|---|---|\n"
    md += "".join(f"| {k} | {v['accepted']} | {v['escalated']} | {v['failed']} | {v['wall_s']:.0f} |\n"
                  for k, v in arms.items())
    return md + ("\n" + extra + "\n" if extra else "")


PILOT = STATE_ROOT / "pilot"
FIDELITY_CMD = ["env", "-u", "ANTHROPIC_API_KEY", "claude", "-p", "--output-format", "json", "--tools", "",
                "--strict-mcp-config", "--setting-sources", "project", "--system-prompt-file",
                str(PROMPTS / "fidelity.md")]


def _private_ollama(ctx: dict):
    import urllib.request
    try:  # already running (e.g. started by hand): reuse it
        urllib.request.urlopen("http://127.0.0.1:11435/api/version", timeout=2)
        return None
    except OSError:
        pass
    env = {**os.environ, "OLLAMA_HOST": "127.0.0.1:11435", "OLLAMA_MODELS": "/usr/share/ollama/.ollama/models",
           "OLLAMA_NOPRUNE": "1", "OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KEEP_ALIVE": "30m",
           "OLLAMA_NUM_PARALLEL": str(ctx.get("parallel", 1)), "OLLAMA_MAX_LOADED_MODELS": "1",
           "OLLAMA_KV_CACHE_TYPE": ctx.get("kv_cache_type", "q8_0")}
    log = open(STATE_ROOT / "pilot-ollama.log", "a")
    return subprocess.Popen(["ollama", "serve"], env=env, stdout=log, stderr=subprocess.STDOUT)


def run_all(seed: int = 20261110):
    import runtime as rtmod
    ctx = json.loads((STATE_ROOT / "ctx.json").read_text())
    tasks = {json.loads(f.read_text())["id"]: json.loads(f.read_text()) for f in sorted((PILOT / "tasks").glob("*.json"))}
    order = plan_order(list(tasks), seed)
    (PILOT / "order.json").write_text(json.dumps({"seed": seed, "order": order}, indent=1))
    srv = _private_ollama(ctx)
    t0 = time.time()
    try:
        for i, (tid, arms) in enumerate(order, 1):
            task = tasks[tid]
            rt = rtmod.ensure_runtime(task["repo"], task["base"], script=task["runtime_script"].encode()) \
                if task.get("runtime_script") else None
            for arm in arms:
                rec = run_one(task, num_ctx=ctx["num_ctx"], runtime=rt) if arm == "mates" else run_solo(task, runtime=rt)
                with open(PILOT / "results.jsonl", "a") as f:
                    f.write(json.dumps(rec) + "\n")
                print(f"[{i}/{len(order)}] {tid} {arm}: {rec['outcome']} lead_tokens={rec.get('lead_tokens')}", flush=True)
            if i == 2:
                per = (time.time() - t0) / 2
                print(f"ETA: {per * (len(order) - 2) / 60:.0f} min more ({per / 60:.1f} min per task)", flush=True)
    finally:
        _stop(srv)
        with open(PILOT / "net.log", "w") as f:  # every sandbox's own-namespace samples
            for nl in sorted((STATE_ROOT / "net").glob("*.log")):
                f.write(nl.read_text())


def fidelity(cmd=FIDELITY_CMD) -> dict:
    import claudejson
    tasks = {json.loads(f.read_text())["id"]: json.loads(f.read_text()) for f in (PILOT / "tasks").glob("*.json")}
    out = {}
    for line in (PILOT / "results.jsonl").read_text().splitlines():
        r = json.loads(line)
        if r["outcome"] != "accepted":
            continue
        t = tasks[r["task_id"]]
        ref = _git(t["repo"], "diff", f"{t['base']}..{t['target']}")
        acc = _git(t["repo"], "diff", f"{t['base']}..{r['commit']}")
        prompt = (f"Brief: {json.dumps(t['brief'])}\n\nReference change:\n{ref[:40000]}\n\n"
                  f"Accepted change:\n{acc[:40000]}\n")
        NEUTRAL_CWD.mkdir(exist_ok=True)
        res = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=600, cwd=str(NEUTRAL_CWD))
        ans = claudejson.parse(res.stdout)["result"]
        out[f"{r['arm']}:{r['task_id']}"] = ans.startswith("CORRECT")
        print(r["arm"], r["task_id"], ans[:200], flush=True)
    (PILOT / "fidelity.json").write_text(json.dumps(out, indent=1))
    return out


def report() -> str:
    recs = [json.loads(l) for l in (PILOT / "results.jsonl").read_text().splitlines()]
    fid = json.loads((PILOT / "fidelity.json").read_text())
    escape_ok = "FAIL" not in (PILOT / "escape.txt").read_text()
    s = score(recs, fid, escape_ok, count_outbound(PILOT / "net.log"))
    arms = {}
    for arm in ("mates", "solo"):
        rs = [r for r in recs if r.get("arm") == arm]
        arms[arm] = {k: sum(1 for r in rs if r["outcome"] == k) for k in ("accepted", "escalated", "failed")}
        arms[arm]["wall_s"] = sum(r.get("worker_wall_s", 0) for r in rs)
    extra = "Context pick: " + (STATE_ROOT / "ctx.json").read_text()
    md = render_report(s, arms, extra)
    (PILOT / "report.md").write_text(md)
    return md


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("run-one")
    o.add_argument("--task", required=True)
    sub.add_parser("run-all")
    sub.add_parser("fidelity")
    sub.add_parser("report")
    a = ap.parse_args()
    if a.cmd == "run-all":
        return run_all()
    if a.cmd == "fidelity":
        return fidelity()
    if a.cmd == "report":
        return print(report())
    if a.cmd == "run-one":
        task = json.loads(pathlib.Path(a.task).read_text())
        ctx = json.loads((STATE_ROOT / "ctx.json").read_text())
        rt = None
        if task.get("runtime_script"):
            import runtime
            rt = runtime.ensure_runtime(task["repo"], task["base"], script=task["runtime_script"].encode())
        print(json.dumps(run_one(task, num_ctx=ctx["num_ctx"], runtime=rt), indent=1))


if __name__ == "__main__":
    main()
