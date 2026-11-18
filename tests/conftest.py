import os
import sys
import pathlib
import subprocess
import time
import uuid
import pytest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "mates"))


def tmux_socket(name):
    return pathlib.Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}" / name


def _listening(name):
    return subprocess.run(["tmux", "-L", name, "list-sessions"], capture_output=True).returncode == 0


@pytest.fixture
def private_tmux():
    """A name for a private `tmux -L` server. Teardown kills it and removes its socket
    (kill-server leaves the socket file behind), and fails the test if it remains."""
    name = f"mates-test-{uuid.uuid4().hex[:8]}"
    yield name
    subprocess.run(["tmux", "-L", name, "kill-server"], capture_output=True)
    for _ in range(50):
        if not _listening(name):
            break
        time.sleep(0.05)
    sock = tmux_socket(name)
    if sock.exists() and not _listening(name):
        sock.unlink()
    assert not sock.exists(), f"tmux socket left behind: {sock}"


@pytest.fixture
def private_dir():
    """A fresh mode-700 folder that passes fsutil.safe_ancestry (under the user runtime
    folder: tmp_path sits under /tmp, which others can write). Removed after."""
    import shutil
    import tempfile
    import fsutil
    d = pathlib.Path(tempfile.mkdtemp(prefix="mates-test-", dir=fsutil._private_base()))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch, request):
    """No test reaches real state (structure review 2026-11-17): cli.LOG and session.STATE_DIR
    are fixed when the modules load, so a test that logs without its own harness wrote Wai's
    real audit log; launch tests started a detached reaper against her real tmux server; and
    the 5 s pane stagger slowed every launch test. Opt back in to the reaper with the
    `real_reaper` marker."""
    import cli
    import session
    monkeypatch.setattr(cli, "LOG", tmp_path / "audit-log.jsonl")
    monkeypatch.setattr(session, "STATE_DIR", tmp_path / "state-sessions")
    if "real_reaper" not in request.keywords:
        monkeypatch.setattr(cli, "start_reaper", lambda root: None)
    monkeypatch.setattr(cli, "PANE_STAGGER_S", 0)


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: end-to-end tests on a private tmux server with a fake OpenCode "
                                       "(tests/e2e; about 20 s; deselect with -m 'not e2e')")
    config.addinivalue_line("markers", "real_reaper: keep cli.start_reaper (the autouse fixture stubs it)")
