"""`mates retro`: the private claude folder, plain flagged names, the lesson-drop cap
(the base retro tests and the `Retro` harness are in test_cli.py)."""
import json
import os
import subprocess
from pathlib import Path

import pytest

import cli
import fsutil
from test_cli import Retro

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def rt(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MATES_LEAD", raising=False)
    return Retro(tmp_path, monkeypatch, capsys)


def test_retro_cwd_is_private_and_removed(rt):
    neutral = Path("/srv/mates/neutral")
    rt.line("2026-11-09")
    assert rt.run("--date", "2026-11-09")[0] == 0
    (c,) = rt.calls()
    cwd = Path(c["cwd"])
    assert cwd.parent == rt.run_dir and cwd != neutral and not str(cwd).startswith(str(neutral))
    assert c["mode"] == 0o700 and c["listing"] == []  # nothing planted (no .claude/settings.json)
    assert not cwd.exists()


def test_private_cwd_refuses_shared_folders(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o770)
    assert not fsutil.safe_ancestry(shared)
    assert not fsutil.safe_ancestry("/tmp")  # everyone's
    if os.path.isdir("/srv/mates/neutral"):
        assert not fsutil.safe_ancestry("/srv/mates/neutral")  # group emetcode
    try:
        base = fsutil._private_base()
    except fsutil.UnsafeDir:
        pytest.skip("no private runtime folder here")
    with fsutil.private_cwd() as d:
        assert Path(d).parent == Path(base) and os.stat(d).st_mode & 0o777 == 0o700 and os.listdir(d) == []
    assert not os.path.exists(d)


def test_retro_refuses_without_private_folder(rt, monkeypatch):
    def none():
        raise fsutil.UnsafeDir("no private runtime folder")
    monkeypatch.setattr(fsutil, "_private_base", none)
    rt.line("2026-11-09")
    code, _, err = rt.run("--date", "2026-11-09")
    assert code == 1 and "private" in err
    assert rt.calls() == [] and not rt.retro_file("2026-11-09").exists()


def test_retro_keeps_lessons_with_plain_flagged_names(rt):
    rt.line("2026-11-09", flags=["unnamed-file:the", "unnamed-file:check", "test-file:Makefile",
                                 "unnamed-file:lib/tasks", "unnamed-file:src/gadget.py", "unnamed-file:frobnicator"])
    rt.reply(lessons=["Name the check in the brief", "Run the Makefile target first", "Split tasks small",
                      "Never touch gadget.py", "Mind the frobnicator", "Keep lib changes small"])
    assert rt.run("--date", "2026-11-09")[0] == 0
    assert rt.kept() == ["Name the check in the brief", "Run the Makefile target first", "Split tasks small",
                         "Keep lib changes small"]


def _old_lessons(rt, n):
    rt.lessons.write_text("## Lessons\n\n" + "".join(f"- old lesson {i}\n" for i in range(n)))
    rt.git("commit", "-qam", "lessons")


def test_retro_drops_at_most_three_lessons(rt):
    _old_lessons(rt, 5)
    rt.line("2026-11-09")
    rt.reply(lessons=["old lesson 0", "one merged lesson"])  # would drop 4
    assert rt.run("--date", "2026-11-09")[0] == 0
    assert rt.kept() == [f"old lesson {i}" for i in range(5)]
    assert rt.git("log", "-1", "--format=%s").strip() == "lessons"
    q = (rt.src / "OPEN-QUESTIONS.md").read_text().split("## Open", 1)[1]
    assert "drops 4 of the current 5" in q and "one merged lesson" in q
    assert "old list stays" in rt.retro_file("2026-11-09").read_text()


def test_retro_may_drop_three_lessons(rt):
    _old_lessons(rt, 5)
    rt.line("2026-11-09")
    rt.reply(lessons=["old lesson 0", "old lesson 1", "new lesson"])  # drops 3
    assert rt.run("--date", "2026-11-09")[0] == 0
    assert rt.kept() == ["old lesson 0", "old lesson 1", "new lesson"]
    assert not (rt.src / "OPEN-QUESTIONS.md").exists()


def test_retro_flags_not_a_list_are_empty(rt):
    rt.line("2026-11-09", flags="unnamed-file:SECRETX", questions="QSECRET")
    rt.reply(lessons=["Keep briefs short"])
    assert rt.run("--date", "2026-11-09")[0] == 0
    s = rt.calls()[0]["stdin"]
    assert "SECRETX" not in s and "QSECRET" not in s
    (rec,) = [json.loads(l) for l in s.split("## Log lines")[1].splitlines() if l.startswith("{")]
    assert rec["flags"] == [] and rec["questions"] == []


def test_bin_mates_retro_runs_outside_a_git_repo(tmp_path):
    (tmp_path / "home").mkdir()
    out = tmp_path / "nowhere"
    out.mkdir()
    env = {**os.environ, "HOME": str(tmp_path / "home"), "MATES_CLAUDE": "/bin/false"}
    env.pop("MATES_LEAD", None)
    r = subprocess.run([str(ROOT / "bin" / "mates"), "retro"], cwd=out, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "nothing to do" in r.stdout
