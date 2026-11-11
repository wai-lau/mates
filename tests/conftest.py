import os, sys, pathlib, subprocess, time, uuid
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
    import shutil, tempfile
    import fsutil
    d = pathlib.Path(tempfile.mkdtemp(prefix="mates-test-", dir=fsutil._private_base()))
    yield d
    shutil.rmtree(d, ignore_errors=True)
