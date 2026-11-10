import json, os, pathlib, subprocess, pytest
import pilot

STUBS = pathlib.Path(__file__).parent / "stubs"
CHECK = "python3 -c \"import sys; sys.path.insert(0,'src'); import calc; assert calc.add(2,2)==4\""

def git(r, *a): return subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True, text=True).stdout.strip()

@pytest.fixture
def env(tmp_path, monkeypatch):
    r = tmp_path / "repo"; (r / "src").mkdir(parents=True)
    (r / "src/calc.py").write_text("def add(a, b):\n    return a - b\n")
    git(r.parent, "init", "-q", str(r)); git(r, "config", "user.email", "w@w"); git(r, "config", "user.name", "w")
    git(r, "add", "-A"); git(r, "commit", "-qm", "base")
    monkeypatch.setenv("STUB_LEAD_COUNTER", str(tmp_path / "lead.count"))
    fwdlog = tmp_path / "fwd.jsonl"
    def fake_fwd(sock, log, model):
        pathlib.Path(log).write_text(json.dumps({"response_model": os.environ.get("STUB_MODEL", "qwen3.8-mates"),
                                                 "input_tokens": 1000, "output_tokens": 200, "status": 200}) + "\n")
        return None
    cfg = dict(claude_bin=str(STUBS / "claude_stub.py"), lead_cmd=[str(STUBS / "lead_stub.py")],
               sandbox=str(STUBS / "sandbox_stub.sh"), tasks_root=tmp_path / "tasks", state_root=tmp_path / "state",
               start_fwd=fake_fwd, num_ctx=65536, timeout=60)
    task = {"id": "t1", "repo": str(r), "base": git(r, "rev-parse", "HEAD"),
            "brief": {"goal": "make add() add", "files": ["src/calc.py"], "constraints": ""}, "check": CHECK}
    return task, cfg, tmp_path

def last_log(tmp): return json.loads((tmp / "state/log.jsonl").read_text().splitlines()[-1])

def test_accept_logs_record(env):
    task, cfg, tmp = env
    rec = pilot.run_one(task, **cfg)
    assert rec["outcome"] == "accepted" and rec["attempts"] == 1 and rec["answered_by"] == ["qwen3.8-mates"]
    assert rec["red_on_base"] is True and rec["flags"] == [] and rec["lead_tokens"] == 115 and rec["lead_cache_read"] == 50
    assert last_log(tmp)["task_id"] == "t1"

def test_green_base_refused(env):
    task, cfg, tmp = env
    task["check"] = "true"
    rec = pilot.run_one(task, **cfg)
    assert rec["outcome"] == "failed" and rec["detail"] == "green-base"

def test_red_on_base_runtime_missing(env):
    task, cfg, tmp = env
    task["check"] = "no-such-tool --run"
    rec = pilot.run_one(task, **cfg)
    assert rec["outcome"] == "failed" and rec["detail"].startswith("runtime-missing")

def test_missing_report_is_failed_attempt(env, monkeypatch):
    task, cfg, tmp = env
    monkeypatch.setenv("STUB_WORKER", "noreport")
    rec = pilot.run_one(task, **cfg)
    assert rec["outcome"] == "escalated" and rec["attempts"] == 2

def test_empty_diff_not_accepted(env, monkeypatch):
    task, cfg, tmp = env
    monkeypatch.setenv("STUB_WORKER", "nochange")
    rec = pilot.run_one(task, **cfg)
    assert rec["outcome"] != "accepted"

def test_commit_is_neutral(env):
    task, cfg, tmp = env
    rec = pilot.run_one(task, **cfg)
    wt = pathlib.Path(task["repo"])
    msg = git(wt, "log", "-1", "--format=%an <%ae>|%B", rec["commit"])
    assert msg.strip() == "mates <mates@localhost>|proposed change t1"

def test_verifier_sees_no_report(env):
    task, cfg, tmp = env
    rec = pilot.run_one(task, **cfg)
    assert rec["verdict"]["pass"] is True  # stub fails the verdict if it can see report.md

def test_rerun_uses_original_check(env, monkeypatch):
    task, cfg, tmp = env
    real = pilot.collect
    def tamper(td, *a, **k):
        out = real(td, *a, **k)
        (pathlib.Path(td) / "ctl/check.sh").write_text("exit 0\n")
        return out
    monkeypatch.setattr(pilot, "collect", tamper)
    monkeypatch.setenv("STUB_WORKER", "nochange")
    rec = pilot.run_one(task, **cfg)
    assert rec["outcome"] != "accepted"

def test_send_back_then_escalate(env, monkeypatch):
    task, cfg, tmp = env
    monkeypatch.setenv("STUB_LEAD", "SEND-BACK: no,SEND-BACK: still no")
    rec = pilot.run_one(task, **cfg)
    assert rec["outcome"] == "escalated" and rec["attempts"] == 2

def test_model_mismatch_fails(env, monkeypatch):
    task, cfg, tmp = env
    monkeypatch.setenv("STUB_MODEL", "gpt-oss:20b")
    rec = pilot.run_one(task, **cfg)
    assert rec["outcome"] != "accepted" and "gpt-oss:20b" in rec["answered_by"]

def test_authorship_flag_forces_diff_read_and_redacts(env, monkeypatch):
    task, cfg, tmp = env
    monkeypatch.setenv("STUB_WORKER", "authorship")
    monkeypatch.setenv("STUB_LEAD", "NEED-DIFF,ACCEPT")
    rec = pilot.run_one(task, **cfg)
    assert "authorship:src/calc.py" in rec["flags"] and rec["full_diff_read"] is True
    diff = (tmp / "state/results/t1.v1.diff").read_text()
    assert "Qwen" not in diff

def test_solo_logs_arm_solo(env):
    task, cfg, tmp = env
    rec = pilot.run_solo(task, claude_cmd=[str(STUBS / "solo_stub.py")], sandbox=cfg["sandbox"],
                         tasks_root=cfg["tasks_root"], state_root=cfg["state_root"])
    assert rec["arm"] == "solo" and rec["route"] == "claude-subagent" and rec["outcome"] == "accepted"
    assert rec["lead_tokens"] == 4000 and rec["lead_cache_read"] == 9000
    assert last_log(tmp)["arm"] == "solo"

def test_solo_failed_check_not_accepted(env, monkeypatch):
    task, cfg, tmp = env
    monkeypatch.setenv("STUB_SOLO", "nochange")
    rec = pilot.run_solo(task, claude_cmd=[str(STUBS / "solo_stub.py")], sandbox=cfg["sandbox"],
                         tasks_root=cfg["tasks_root"], state_root=cfg["state_root"])
    assert rec["outcome"] == "failed"

def test_red_on_base_ignores_unrelated_no_such_file(env):
    task, cfg, tmp = env
    task["check"] = "echo 'FileNotFoundError: [Errno 2] No such file or directory' >&2; exit 1"
    rec = pilot.run_one(task, **cfg)
    assert rec["red_on_base"] is True and not rec.get("detail", "").startswith("runtime-missing")

def test_task_dirs_group_writable(env):
    task, cfg, tmp = env
    c = pilot.Ctx(task, cfg["claude_bin"], cfg["lead_cmd"], cfg["sandbox"], cfg["tasks_root"], cfg["state_root"],
                  cfg["start_fwd"], 65536, 60)
    import os
    os.umask(0o022)
    td = pilot.make_task_dir(c, "perm", task["base"], "true\n")
    assert all((td / d).stat().st_mode & 0o020 for d in ("sock", "state"))
    pilot._cleanup(c)
