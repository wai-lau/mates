import json
import os
import shutil
import sqlite3
import subprocess
import time
import uuid

import pytest

import layout
import session


@pytest.fixture(autouse=True)
def _dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "STATE_DIR", tmp_path / "sessions")
    monkeypatch.setattr(session, "PROJECTS_DIR", tmp_path / "projects")


def _stub_tmux(tmp_path, monkeypatch, body):
    p = tmp_path / "tmux"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    monkeypatch.setattr(layout, "TMUX", str(p))
    return p


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


def _task(**kw):
    t = {"id": "t1", "summary": "fix add", "reason": None, "files": ["calc.py"], "base": "abc",
         "direct": False, "sent_ts": 1.0, "attempt_ts": 1.0, "attempts": 1, "opencode_session": "ses_1",
         "questions": [{"q": "which?", "a": "add"}], "diffed_tree": None, "flags": [],
         "checked_tree": None, "check_cmd": None}
    t.update(kw)
    return t


def _qwen(n, **kw):
    q = {"n": n, "worktree": f"/r/.claude/worktrees/mates-qwen-{n}", "branch": f"mates/qwen-{n}",
         "opencode_session": None, "closed": False, "task": None}
    q.update(kw)
    return q


# --- state file ---------------------------------------------------------------

def test_repo_key_matches_claude_projects():
    assert session.repo_key("/home/wai/.claude/mates") == "-home-wai--claude-mates"


def test_roundtrip(tmp_path):
    root = "/r"
    st = session.new_state(root)
    assert st["parallel"] is session.PARALLEL_DEFAULT is False
    assert st["claude_title"] == "mates r"
    st["claude_session"] = str(uuid.uuid4())
    st["widths"] = [{"pane": "lead", "pct": 50}, {"pane": 1, "pct": 50}]
    st["qwens"] = [_qwen(1, task=_task()), _qwen(2, closed=True)]
    assert session.load(root) is None
    session.save(root, st)
    assert session.load(root) == st
    f = session.STATE_DIR / (session.repo_key(root) + ".json")
    assert json.loads(f.read_text()) == st
    assert [p.name for p in session.STATE_DIR.iterdir() if p.name.startswith(".")] == []


def test_save_never_drops_entries():
    root = "/r"
    st = session.new_state(root)
    st["qwens"] = [_qwen(1), _qwen(2), _qwen(3)]
    session.save(root, st)
    later = session.new_state(root)
    later["qwens"] = [_qwen(2, opencode_session="ses_x")]
    session.save(root, later)
    got = session.load(root)
    assert [q["n"] for q in got["qwens"]] == [1, 2, 3]
    assert got["qwens"][1]["opencode_session"] == "ses_x"


def test_restore_after_all_panes_gone(tmp_path, monkeypatch):
    _stub_tmux(tmp_path, monkeypatch, "exit 0\n")  # a tmux with no panes at all
    root = "/r"
    st = session.new_state(root)
    st["qwens"] = [_qwen(1), _qwen(2, task=_task())]
    session.save(root, st)
    assert session.find_pane(root, 1) is None and session.find_pane(root, 2) is None
    session.save(root, session.load(root))
    got = session.load(root)
    assert [q["n"] for q in got["qwens"]] == [1, 2]
    assert all(q["closed"] is False for q in got["qwens"])
    assert got["qwens"][1]["task"] == _task()


# --- panes by tag -----------------------------------------------------------------

def _listing(tmp_path, monkeypatch, rows):
    out = "".join(f"printf '%s\\t%s\\t%s\\n' '{a}' '{b}' '{c}'\n" for a, b, c in rows)
    return _stub_tmux(tmp_path, monkeypatch, out)


def test_find_pane_by_tag(tmp_path, monkeypatch):
    key = session.repo_key("/r")
    _listing(tmp_path, monkeypatch, [
        ("%1", f"{key}:lead", ""),
        ("%2", "-other-repo:1", ""),      # pane id reused by another repo's mates
        ("%3", f"{key}:2", ""),
        ("%4", "", ""),
    ])
    assert session.find_lead("/r") == "%1"
    assert session.find_pane("/r", 2) == ("%3", "ok")
    assert session.find_pane("/r", 1) is None
    assert session.find_lead("/other/repo") is None


def test_find_pane_no_tmux_server(tmp_path, monkeypatch):
    _stub_tmux(tmp_path, monkeypatch, "echo 'no server running' >&2; exit 1\n")
    assert session.find_pane("/r", 1) is None
    assert session.find_lead("/r") is None


def test_failed_pane_is_failed_not_closed(tmp_path, monkeypatch):
    key = session.repo_key("/r")
    _listing(tmp_path, monkeypatch, [("%5", f"{key}:1", "1")])
    assert session.find_pane("/r", 1) == ("%5", "failed")
    st = session.new_state("/r")
    st["qwens"] = [_qwen(1)]
    assert session.mark_closed(st, 1) is False
    assert st["qwens"][0]["closed"] is False


def test_mark_closed_only_when_gone(tmp_path, monkeypatch):
    key = session.repo_key("/r")
    _listing(tmp_path, monkeypatch, [("%5", f"{key}:1", "")])
    st = session.new_state("/r")
    st["qwens"] = [_qwen(1), _qwen(2)]
    assert session.mark_closed(st, 2) is False   # pane 2 is gone, but so is the lead: not Wai closing it
    _listing(tmp_path, monkeypatch, [("%5", f"{key}:1", ""), ("%4", f"{key}:lead", "")])
    assert session.mark_closed(st, 1) is False   # pane 1 is live
    assert session.mark_closed(st, 2) is True    # pane 2 is gone while the lead lives
    assert [q["closed"] for q in st["qwens"]] == [False, True]


def test_tag_pane_sets_and_clears_failed(tmp_path, monkeypatch):
    log = tmp_path / "calls"
    _stub_tmux(tmp_path, monkeypatch, f'echo "$@" >> {log}\n')
    session.tag_pane("%7", "/r", 3)
    session.tag_pane("%1", "/r", "lead")
    key = session.repo_key("/r")
    assert log.read_text().splitlines() == [
        f"set-option -p -t %7 @mates {key}:3", "set-option -p -u -t %7 @mates-failed",
        f"set-option -p -t %1 @mates {key}:lead", "set-option -p -u -t %1 @mates-failed"]


@pytest.mark.parametrize("fail_on", ["@mates", "@mates-failed"])
def test_tag_pane_raises_when_set_option_fails(tmp_path, monkeypatch, fail_on):
    _stub_tmux(tmp_path, monkeypatch,
               f'for a in "$@"; do [ "$a" = "{fail_on}" ] && {{ echo "no such pane" >&2; exit 1; }}; done\nexit 0\n')
    with pytest.raises(RuntimeError, match="no such pane"):
        session.tag_pane("%7", "/r", 3)


@pytest.mark.skipif(not shutil.which("tmux"), reason="needs tmux")
def test_tag_and_find_real_tmux(tmp_path, monkeypatch, private_tmux):
    sock = private_tmux
    wrap = tmp_path / "tmux"
    wrap.write_text(f'#!/bin/sh\nexec tmux -L {sock} -f /dev/null "$@"\n')
    wrap.chmod(0o755)
    monkeypatch.setattr(layout, "TMUX", str(wrap))
    try:
        pane = subprocess.run([str(wrap), "new-session", "-d", "-P", "-F", "#{pane_id}", "sleep 60"],
                              check=True, capture_output=True, text=True).stdout.strip()
        session.tag_pane(pane, "/r", 1)
        assert session.find_pane("/r", 1) == (pane, "ok")
        subprocess.run([str(wrap), "set-option", "-p", "-t", pane, "@mates-failed", "1"], check=True)
        assert session.find_pane("/r", 1) == (pane, "failed")
        session.tag_pane(pane, "/r", 1)
        assert session.find_pane("/r", 1) == (pane, "ok")
        assert session.find_pane("/r", 2) is None
    finally:
        subprocess.run([str(wrap), "kill-server"], capture_output=True)  # the fixture removes the socket


# --- worktrees ----------------------------------------------------------------------

def _excludes(root):
    p = _git(root, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude")
    return open(p).read().splitlines()


def test_ensure_worktree_idempotent(tmp_path):
    root = _repo(tmp_path / "repo")
    path, branch = session.ensure_worktree(root, 1)
    assert path == root / ".claude/worktrees/mates-qwen-1" and branch == "mates/qwen-1"
    assert _git(path, "rev-parse", "--abbrev-ref", "HEAD") == "mates/qwen-1"
    assert _git(path, "rev-parse", "HEAD") == _git(root, "rev-parse", "HEAD")
    (path / "work.txt").write_text("kept\n")
    assert session.ensure_worktree(root, 1) == (path, branch)
    assert (path / "work.txt").read_text() == "kept\n"
    ex = _excludes(root)
    assert ex.count(".mates/") == 1 and ex.count("/.claude/worktrees/") == 1
    assert _git(root, "status", "--porcelain") == ""


def test_ensure_worktree_when_root_is_a_worktree(tmp_path):
    main = _repo(tmp_path / "main")
    root = tmp_path / "lead"
    _git(main, "worktree", "add", "-q", "-b", "lead", str(root))
    assert (root / ".git").is_file()
    path, branch = session.ensure_worktree(root, 2)
    assert path == root / ".claude/worktrees/mates-qwen-2" and branch == "mates/qwen-2"
    assert session.ensure_worktree(root, 2) == (path, branch)
    assert _git(path, "rev-parse", "--abbrev-ref", "HEAD") == "mates/qwen-2"
    ex = _excludes(root)
    assert ex.count(".mates/") == 1 and ex.count("/.claude/worktrees/") == 1
    assert _git(root, "status", "--porcelain") == ""


def test_ensure_worktree_reuses_branch_after_dir_removed(tmp_path):
    root = _repo(tmp_path / "repo")
    path, _ = session.ensure_worktree(root, 1)
    (path / "b.txt").write_text("b\n")
    _git(path, "add", "b.txt")
    _git(path, "commit", "-q", "-m", "qwen work")
    head = _git(path, "rev-parse", "HEAD")
    shutil.rmtree(path)
    path2, _ = session.ensure_worktree(root, 1)
    assert path2 == path and _git(path, "rev-parse", "HEAD") == head


# --- OpenCode ----------------------------------------------------------------------

def _oc_db(path):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE session (id text PRIMARY KEY, project_id text NOT NULL, parent_id text,
            directory text NOT NULL, title text NOT NULL, time_created integer NOT NULL,
            time_updated integer NOT NULL);
        CREATE TABLE message (id text PRIMARY KEY, session_id text NOT NULL,
            time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL);
        CREATE TABLE part (id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL,
            time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL);
    """)
    return db


def _ses(db, sid, directory, updated, parent=None):
    db.execute("INSERT INTO session VALUES (?, 'p', ?, ?, 't', ?, ?)", (sid, parent, directory, updated, updated))
    db.commit()


def _msg(db, mid, sid, created_s, role, completed_s=None, finish=None, updated_s=None):
    data = {"role": role, "time": {"created": int(created_s * 1000)}}
    if completed_s is not None:
        data["time"]["completed"] = int(completed_s * 1000)
    if finish:
        data["finish"] = finish
    up = updated_s if updated_s is not None else (completed_s or created_s)
    db.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)",
               (mid, sid, int(created_s * 1000), int(up * 1000), json.dumps(data)))
    db.commit()


def _part(db, pid, mid, sid, updated_s):
    db.execute("INSERT INTO part VALUES (?, ?, ?, ?, ?, '{}')", (pid, mid, sid, int(updated_s * 1000), int(updated_s * 1000)))
    db.commit()


def test_opencode_session_newest_for_dir(tmp_path):
    p = tmp_path / "opencode.db"
    db = _oc_db(p)
    _ses(db, "ses_old", "/w/1", 1000)
    _ses(db, "ses_new", "/w/1", 3000)
    _ses(db, "ses_child", "/w/1", 9000, parent="ses_new")   # a subagent session
    _ses(db, "ses_other", "/w/2", 8000)
    assert session.opencode_session("/w/1", p) == "ses_new"
    assert session.opencode_session("/w/3", p) is None
    assert session.opencode_session("/w/1", tmp_path / "missing.db") is None
    assert not (tmp_path / "missing.db").exists()


def test_opencode_db_runs_qwen_in_root(tmp_path, monkeypatch):
    stub = tmp_path / "qwen"
    stub.write_text('#!/bin/sh\n[ "$1" = opencode-dir ] || exit 2\necho "$PWD/oc"\n')
    stub.chmod(0o755)
    monkeypatch.setenv("MATES_QWEN", str(stub))
    root = tmp_path / "root"
    root.mkdir()
    assert session.opencode_db(root) == root / "oc" / "opencode.db"


def test_opencode_idle_after_completed_reply(tmp_path):
    now = time.time()
    p = tmp_path / "opencode.db"
    db = _oc_db(p)
    _msg(db, "m1", "s", now - 20, "user")
    _msg(db, "m2", "s", now - 19, "assistant", completed_s=now - 5, finish="stop")
    assert session.opencode_state("s", p, now - 30) == "idle"
    # the completed reply is older than the paste: still busy
    assert session.opencode_state("s", p, now - 10) == "busy"


def test_opencode_busy_mid_reply(tmp_path):
    now = time.time()
    p = tmp_path / "opencode.db"
    db = _oc_db(p)
    _msg(db, "m1", "s", now - 20, "user")
    _msg(db, "m2", "s", now - 19, "assistant", completed_s=now - 15, finish="tool-calls")
    assert session.opencode_state("s", p, now - 30) == "busy"   # a tool step ended, the reply did not
    _msg(db, "m3", "s", now - 14, "assistant")
    assert session.opencode_state("s", p, now - 30) == "busy"


def test_busy_right_after_paste(tmp_path):
    now = time.time()
    p = tmp_path / "opencode.db"
    db = _oc_db(p)
    _msg(db, "m1", "s", now - 900, "user")
    _msg(db, "m2", "s", now - 899, "assistant", completed_s=now - 800, finish="stop")
    assert session.opencode_state("s", p, now) == "busy"
    assert session.opencode_state(None, p, now) == "busy"   # session row not there yet


def test_busy_expires_without_updates(tmp_path):
    now = time.time()
    p = tmp_path / "opencode.db"
    db = _oc_db(p)
    _msg(db, "m1", "s", now - 1000, "user")
    _msg(db, "m2", "s", now - 999, "assistant", updated_s=now - 400)   # never completed: crashed
    _part(db, "p1", "m2", "s", now - 350)
    assert session.opencode_state("s", p, now - 1000) == "idle"
    assert session.opencode_state("s", p, now - 200) == "busy"         # paste too recent
    _part(db, "p2", "m2", "s", now - 10)                               # still streaming
    assert session.opencode_state("s", p, now - 1000) == "busy"


# --- Claude ---------------------------------------------------------------------------

def _transcript(path, titles, mtime):
    lines = [{"type": "user", "message": {"content": "hi"}}]
    lines += [{"type": "custom-title", "customTitle": t} for t in titles]
    path.write_text("".join(json.dumps(l) + "\n" for l in lines))
    os.utime(path, (mtime, mtime))


def test_claude_session_follows_title(tmp_path):
    root = "/home/wai/src/x"
    proj = session.PROJECTS_DIR / session.repo_key(root)
    proj.mkdir(parents=True)
    _transcript(proj / "aaa.jsonl", ["mates x"], 1000)
    _transcript(proj / "bbb.jsonl", ["mates x"], 2000)          # newest titled: wins
    _transcript(proj / "ccc.jsonl", ["mates x", "other"], 3000)  # retitled away
    _transcript(proj / "ddd.jsonl", [], 4000)                    # untitled
    assert session.claude_session(root, "mates x") == "bbb"
    assert session.claude_session(root, "nope") is None
    assert session.claude_session("/no/such", "mates x") is None
