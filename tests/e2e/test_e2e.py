"""End-to-end: real `mates` commands, a private tmux server, a temp git repo, and a stand-in
for OpenCode (fake_opencode.py) that draws OpenCode's screen and writes its database rows.

Nothing here touches the owner's tmux server, ~/.claude or a model: HOME is a temp folder,
tmux runs on a private socket (a `tmux` wrapper first on PATH), and MATES_QWEN points at a
stub that starts the fake (and runs checks without the sandbox: bin/qwen's sandbox has its
own tests in tests/test_qwen_bin.sh).
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import session

HERE = Path(__file__).resolve().parent
CLI = HERE.parent.parent / "mates" / "cli.py"
TMUX = shutil.which("tmux")

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(TMUX is None, reason="needs tmux")]


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class World:
    def __init__(self, tmp_path, private_tmux, private_dir, monkeypatch):
        self.t = tmp_path
        home = tmp_path / "home"
        home.mkdir()
        self.root = tmp_path / "repo"
        self.root.mkdir()
        _git(self.root, "init", "-q", "-b", "main")
        (self.root / "a.txt").write_text("a\n")
        _git(self.root, "add", "a.txt")
        _git(self.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
        wrap = tmp_path / "wrap"
        wrap.mkdir()
        (wrap / "tmux").write_text(f'#!/bin/sh\nexec {TMUX} -L {private_tmux} -f /dev/null "$@"\n')
        (wrap / "tmux").chmod(0o755)
        dbdir = tmp_path / "oc"
        qwen = tmp_path / "qwen"
        qwen.write_text(f"""#!/bin/sh
case $1 in
  opencode-dir) echo '{dbdir}' ;;
  exec) shift; [ "$1" = -- ] && shift; cd "${{QWEN_CHECK_DIR:?}}" && exec "$@" ;;
  host) exit 1 ;;
  warm) exit 0 ;;
  *) exec {sys.executable} {HERE / 'fake_opencode.py'} ;;
esac
""")
        qwen.chmod(0o755)
        run = private_dir / "run"
        run.mkdir(mode=0o700)
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE", "ANTHROPIC_API_KEY")}
        self.env.update(HOME=str(home), PATH=f"{wrap}:{os.environ['PATH']}", MATES_QWEN=str(qwen),
                        MATES_ROOT=str(self.root), MATES_LEAD="1", XDG_STATE_HOME=str(private_dir / "state"),
                        XDG_RUNTIME_DIR=str(run), FAKE_OPENCODE_DB=str(dbdir / "opencode.db"),
                        FAKE_OPENCODE_WORK_S="0.5", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        # the lead pane, tagged, and a saved state, as `mates launch` would leave them
        self.tmux("new-session", "-d", "-s", "e2e", "-x", "200", "-y", "50", "sleep 100000")
        lead = self.tmux("display", "-p", "-t", "e2e", "#{pane_id}")
        monkeypatch.setattr(session, "STATE_DIR", home / ".claude" / "mates" / "sessions")
        self.key = session.repo_key(str(self.root))
        self.tmux("set-option", "-p", "-t", lead, "@mates", f"{self.key}:lead")
        session.save(str(self.root), session.new_state(str(self.root)))

    def tmux(self, *args):
        r = subprocess.run(["tmux", *args], env=self.env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def mates(self, *args, ok=True):
        r = subprocess.run([sys.executable, str(CLI), *map(str, args)], env=self.env, cwd=self.root,
                           capture_output=True, text=True, timeout=120)
        if ok:
            assert r.returncode == 0, r.stderr + r.stdout
        return r

    def pane(self, n=1):
        hit = None
        for line in self.tmux("list-panes", "-a", "-F", "#{pane_id} #{@mates}").splitlines():
            pid, tag = [*line.split(" ", 1), ""][:2]
            if tag == f"{self.key}:{n}":
                hit = pid
        return hit

    def screen(self, n=1):
        return self.tmux("capture-pane", "-p", "-t", self.pane(n))

    def until(self, cond, s=15):
        end = time.time() + s
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.1)
        raise AssertionError("timed out waiting")

    def add(self):
        self.mates("add")
        self.until(lambda: self.pane() and "Ask anything" in self.screen())


@pytest.fixture
def w(tmp_path, private_tmux, private_dir, monkeypatch):
    return World(tmp_path, private_tmux, private_dir, monkeypatch)


def test_send_done_check_diff_merge(w):
    w.add()
    w.mates("send", 1, "--reason", "e2e", "--files", "a.txt", "Make a.txt say b. FAKE:done")
    out = w.mates("wait", 1, "--timeout", "60").stdout
    assert out.startswith("done: a.txt: b")
    assert "check exit 0" in w.mates("check", 1, "--", "grep -qx b a.txt").stdout
    assert "+b" in w.mates("diff", 1).stdout
    w.mates("merge", 1, "-m", "e2e: a.txt says b")
    assert (w.root / "a.txt").read_text() == "b\n" and _git(w.root, "status", "--porcelain") == ""


def test_question_then_answer(w):
    w.add()
    w.mates("send", 1, "--reason", "e2e", "--files", "a.txt", "Pick a value. FAKE:question")
    assert w.mates("wait", 1, "--timeout", "60").stdout.startswith("question: Which value?")
    w.mates("answer", 1, "Use b. FAKE:done")
    assert w.mates("wait", 1, "--timeout", "60").stdout.startswith("done:")


def test_input_box_guard(w):
    w.add()
    w.tmux("send-keys", "-t", w.pane(), "-l", "half a sentence")
    w.until(lambda: "half a sentence" in w.screen())
    r = w.mates("send", 1, "--reason", "e2e", "task", ok=False)
    assert r.returncode != 0 and "input box" in r.stderr
    w.tmux("send-keys", "-t", w.pane(), *["BSpace"] * len("half a sentence"))
    w.until(lambda: "half a sentence" not in w.screen())
    w.mates("send", 1, "--reason", "e2e", "task")


def test_clear_box_deletes_typed_text_and_sends(w):
    w.add()
    w.tmux("send-keys", "-t", w.pane(), "-l", "half a sentence")
    w.until(lambda: "half a sentence" in w.screen())
    r = w.mates("send", 1, "--reason", "e2e", "--files", "a.txt", "Make a.txt say b. FAKE:done", ok=False)
    assert r.returncode != 0 and "half a sentence" in r.stderr and "--clear-box" in r.stderr
    w.mates("send", 1, "--reason", "e2e", "--files", "a.txt", "--clear-box", "Make a.txt say b. FAKE:done")
    assert w.mates("wait", 1, "--timeout", "60").stdout.startswith("done: a.txt: b")


def test_wait_stops_a_qwen_only_opencode_kept_going(w):
    """docs/bugs.md, 2026-11-16: done.md, then OpenCode compacted and continued Qwen itself;
    wait reports done and stops the pane instead of waiting hours for it."""
    w.add()
    w.mates("send", 1, "--reason", "e2e", "--files", "a.txt", "Make a.txt say b. FAKE:continue")
    out = w.mates("wait", 1, "--timeout", "60").stdout.splitlines()
    assert out[0] == "done: a.txt: b" and out[-1].startswith("mates: stopped Qwen pane 1")
    w.until(lambda: "interrupt" not in w.screen())
    assert "check exit 0" in w.mates("check", 1, "--", "grep -qx b a.txt").stdout


def test_discard_stops_a_reply_with_two_escapes(w):
    """OpenCode's second Escape takes only once the first has armed the interrupt."""
    w.add()
    w.mates("send", 1, "--reason", "e2e", "--files", "a.txt", "Work forever. FAKE:hang")
    w.until(lambda: "esc interrupt" in w.screen())
    w.mates("discard", 1)
    assert "interrupt" not in w.screen()


def test_killed_pane_reads_dead(w):
    w.add()
    w.tmux("kill-pane", "-t", w.pane())
    assert w.mates("wait", 1, "--timeout", "10").stdout == "dead\n"
    assert "closed" in w.mates("list").stdout or "yes" in w.mates("list").stdout
