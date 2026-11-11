import json
import sqlite3
import subprocess
import uuid

import pytest

import cli
import layout
import session


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "STATE_DIR", tmp_path / "sessions")
    monkeypatch.setattr(session, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # no ~/.claude/private.md
    monkeypatch.delenv("MATES_CLAUDE", raising=False)
    monkeypatch.delenv("MATES_LEAD", raising=False)
    oc = tmp_path / "oc"
    oc.mkdir()
    q = tmp_path / "qwen"
    q.write_text(f"#!/bin/sh\ncase $1 in opencode-dir) echo '{oc}' ;; warm) echo warm >> '{tmp_path}/warm.log' ;; esac\n")
    q.chmod(0o755)
    monkeypatch.setenv("MATES_QWEN", str(q))


def _oc_db(tmp_path, rows):
    db = sqlite3.connect(tmp_path / "oc" / "opencode.db")
    db.execute("CREATE TABLE session (id text PRIMARY KEY, project_id text NOT NULL, parent_id text, "
               "directory text NOT NULL, title text NOT NULL, time_created integer NOT NULL, "
               "time_updated integer NOT NULL)")
    for sid, directory in rows:
        db.execute("INSERT INTO session VALUES (?, 'p', NULL, ?, 't', 1, 1)", (sid, directory))
    db.commit()
    db.close()


def _transcript(root, sid, title):
    d = session.PROJECTS_DIR / session.repo_key(root)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text(json.dumps({"type": "custom-title", "customTitle": title}) + "\n")


def _wt(root, n):
    return f"{root}/.claude/worktrees/mates-qwen-{n}"


def _q(root, n, **kw):
    q = {"n": n, "worktree": _wt(root, n), "branch": f"mates/qwen-{n}", "opencode_session": None,
         "closed": False, "task": None}
    q.update(kw)
    return q


def _after(argv, flag):
    return argv[argv.index(flag) + 1]


def _root(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    return str(r)


def test_launch_plan_fresh(tmp_path):
    root = _root(tmp_path)
    plan = cli.launch_plan(None, root, False, [])
    argv = plan["claude_argv"]
    assert argv[:6] == ["env", "-u", "ANTHROPIC_API_KEY", "MATES_LEAD=1", f"MATES_ROOT={root}", "claude"]
    sid = _after(argv, "--session-id")
    assert str(uuid.UUID(sid)) == sid
    assert _after(argv, "-n") == "mates r"
    assert "--resume" not in argv
    prompt = _after(argv, "--append-system-prompt")
    assert "[from Claude]" in prompt and cli.ASK_SUFFIX in prompt and not prompt.endswith("\n")
    assert [(q["n"], q["worktree"]) for q in plan["qwens"]] == [(1, _wt(root, 1))]
    cmd = plan["qwens"][0]["cmd"]
    assert f"QWEN_ROOT={_wt(root, 1)}" in cmd and "QWEN_GIT_RO=1" in cmd and " -s " not in cmd
    assert plan["warnings"] == []
    st = plan["state"]
    assert st["claude_session"] == sid
    assert [(q["n"], q["closed"]) for q in st["qwens"]] == [(1, False)]


def test_fresh_closes_entries_above_one(tmp_path):
    root = _root(tmp_path)
    old = session.new_state(root)
    old["claude_session"] = "old-sid"
    _transcript(root, "old-sid", "mates r")
    old["qwens"] = [_q(root, 1, opencode_session="ses_1", task={"id": "t"}), _q(root, 2), _q(root, 3)]
    plan = cli.launch_plan(old, root, True, [])
    assert _after(plan["claude_argv"], "--session-id") != "old-sid"
    assert [q["n"] for q in plan["qwens"]] == [1]
    assert " -s " not in plan["qwens"][0]["cmd"]
    by_n = {q["n"]: q for q in plan["state"]["qwens"]}
    assert (by_n[1]["closed"], by_n[1]["opencode_session"], by_n[1]["task"]) == (False, None, None)
    assert by_n[2]["closed"] and by_n[3]["closed"]
    assert old["qwens"][1]["closed"] is False  # the input is not changed


def test_launch_plan_restore(tmp_path):
    root = _root(tmp_path)
    _transcript(root, "sid-1", "mates r")
    _oc_db(tmp_path, [("ses_1", _wt(root, 1)), ("ses_3", _wt(root, 3))])
    old = session.new_state(root)
    old["claude_session"] = "sid-1"
    old["widths"] = [{"pane": "lead", "pct": 50}, {"pane": 3, "pct": 25}, {"pane": 1, "pct": 25}]
    old["qwens"] = [_q(root, 1, opencode_session="ses_1"), _q(root, 2, closed=True),
                    _q(root, 3, opencode_session="ses_3")]
    plan = cli.launch_plan(old, root, False, [])
    argv = plan["claude_argv"]
    assert _after(argv, "--resume") == "sid-1" and "--session-id" not in argv
    assert [q["n"] for q in plan["qwens"]] == [3, 1]  # the saved left-to-right order
    assert plan["qwens"][0]["cmd"].count("-s ses_3") == 1 and "-s ses_1" in plan["qwens"][1]["cmd"]
    assert plan["warnings"] == []
    by_n = {q["n"]: q for q in plan["state"]["qwens"]}
    assert by_n[2]["closed"] and not by_n[1]["closed"] and not by_n[3]["closed"]


def test_restore_prefers_newest_titled_transcript(tmp_path):
    root = _root(tmp_path)
    _transcript(root, "after-clear", "mates r")  # /clear made a new transcript with the same title
    old = session.new_state(root)
    old["claude_session"] = "before-clear"
    old["qwens"] = [_q(root, 1)]
    plan = cli.launch_plan(old, root, False, [])
    assert _after(plan["claude_argv"], "--resume") == "after-clear"
    assert plan["state"]["claude_session"] == "after-clear"


def test_restore_missing_session_fresh(tmp_path):
    root = _root(tmp_path)
    _oc_db(tmp_path, [("ses_2", _wt(root, 2))])
    old = session.new_state(root)
    old["claude_session"] = "gone-sid"
    old["qwens"] = [_q(root, 1, opencode_session="ses_gone"), _q(root, 2, opencode_session="ses_2")]
    plan = cli.launch_plan(old, root, False, [])
    argv = plan["claude_argv"]
    assert "--resume" not in argv and _after(argv, "--session-id") != "gone-sid"
    cmds = {q["n"]: q["cmd"] for q in plan["qwens"]}
    assert " -s " not in cmds[1] and "-s ses_2" in cmds[2]
    assert len(plan["warnings"]) == 2
    assert any("gone-sid" in w for w in plan["warnings"])
    assert any("ses_gone" in w and "pane 1" in w for w in plan["warnings"])
    assert {q["n"]: q["opencode_session"] for q in plan["state"]["qwens"]} == {1: None, 2: "ses_2"}


def test_launch_plan_passes_claude_args(tmp_path):
    plan = cli.launch_plan(None, _root(tmp_path), False, ["--model", "x"])
    assert plan["claude_argv"][-2:] == ["--model", "x"]


def test_launch_plan_runtime_reaches_every_pane(tmp_path):
    root = _root(tmp_path)
    plan = cli.launch_plan(None, root, False, [], runtime_dir="/srv/rt/abc")
    assert "QWEN_RUNTIME=/srv/rt/abc" in plan["qwens"][0]["cmd"]
    assert plan["state"]["runtime"] == "/srv/rt/abc"


def test_private_md_appended_when_readable(tmp_path):
    p = tmp_path / "home" / ".claude" / "private.md"
    p.parent.mkdir(parents=True)
    p.write_text("PRIVATE-NOTE\n")
    plan = cli.launch_plan(None, _root(tmp_path), False, [])
    assert _after(plan["claude_argv"], "--append-system-prompt").endswith("PRIVATE-NOTE")


def test_subcommand_refused_without_lead_env(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["send", "1", "--reason", "r", "do it"])
    assert e.value.code != 0
    assert "MATES_LEAD" in capsys.readouterr().err
    for open_cmd in ("list", "log", "retro"):
        with pytest.raises(SystemExit):
            cli.main([open_cmd])
        assert "MATES_LEAD" not in capsys.readouterr().err


# --- launch against a stub tmux and a real repo ---------------------------------------

def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _repo(path):
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "a.txt").write_text("a\n")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _stub_tmux(tmp_path, monkeypatch):
    log = tmp_path / "tmux.log"
    p = tmp_path / "tmux"
    p.write_text(f"""#!/bin/sh
printf '%s\\n' "$*" >> '{log}'
case $1 in
  split-window) n=$(cat '{tmp_path}/n' 2>/dev/null || echo 1); echo $((n + 1)) > '{tmp_path}/n'; echo "%$n" ;;
  list-panes) ;;
esac
exit 0
""")
    p.chmod(0o755)
    monkeypatch.setattr(layout, "TMUX", str(p))
    return log


def test_fresh_keeps_worktrees(tmp_path, monkeypatch, capsysbinary):
    root = _repo(tmp_path / "repo")
    log = _stub_tmux(tmp_path, monkeypatch)
    for n in (1, 2):
        session.ensure_worktree(root, n)
    wt1, wt2 = root / ".claude/worktrees/mates-qwen-1", root / ".claude/worktrees/mates-qwen-2"
    (wt2 / "a.txt").write_text("qwen's work\n")
    (wt2 / "new.py").write_text("x = 1\n")
    (wt1 / ".mates").mkdir()
    (wt1 / ".mates" / "done.md").write_text("done\n")  # protocol files are not work
    old = session.new_state(str(root))
    old["qwens"] = [_q(str(root), 1), _q(str(root), 2)]
    session.save(str(root), old)
    monkeypatch.chdir(root)
    monkeypatch.setenv("TMUX_PANE", "%0")

    assert cli.worktree_dirty(str(root), 1) == []
    assert sorted(cli.worktree_dirty(str(root), 2)) == ["a.txt", "new.py"]

    assert cli.main(["launch", "--fresh"]) == 0
    out = capsysbinary.readouterr()
    err = out.err.decode()
    assert "mates-qwen-2" in err and "a.txt" in err and "new.py" in err
    assert "mates-qwen-1" not in err
    assert (wt2 / "a.txt").read_text() == "qwen's work\n" and (wt2 / "new.py").exists()
    argv = out.out.split(b"\0")
    assert argv[-1] == b"" and argv[0] == b"env" and b"--session-id" in argv
    st = session.load(str(root))
    assert {q["n"]: q["closed"] for q in st["qwens"]} == {1: False, 2: True}
    calls = log.read_text()
    assert "split-window -h -P -F #{pane_id} -t %0 -l 50%" in calls
    assert f"QWEN_ROOT={wt1}" in calls
    assert "wait-for -S mates-go-%1" in calls
    assert f"set-option -p -t %1 @mates {session.repo_key(str(root))}:1" in calls
    assert f"set-option -p -t %0 @mates {session.repo_key(str(root))}:lead" in calls
    assert f"set-option -w -t %0 @mates-window {session.repo_key(str(root))}" in calls


def test_restore_reapplies_widths(tmp_path, monkeypatch, capsysbinary):
    root = _repo(tmp_path / "repo")
    log = _stub_tmux(tmp_path, monkeypatch)
    old = session.new_state(str(root))
    old["widths"] = [{"pane": "lead", "pct": 40}, {"pane": 2, "pct": 30}, {"pane": 1, "pct": 30}]
    old["qwens"] = [_q(str(root), 1), _q(str(root), 2)]
    session.save(str(root), old)
    monkeypatch.chdir(root)
    monkeypatch.setenv("TMUX_PANE", "%0")
    assert cli.main(["launch"]) == 0
    calls = log.read_text().splitlines()
    splits = [c for c in calls if c.startswith("split-window")]
    assert "mates-qwen-2" in splits[0] and "-t %0 -l 50%" in splits[0]  # saved order: 2 then 1
    assert "mates-qwen-1" in splits[1] and "-t %1 " in splits[1]
    assert [c for c in calls if c.startswith("resize-pane")] == ["resize-pane -t %0 -x 40%", "resize-pane -t %1 -x 30%"]
