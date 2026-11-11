"""Private folders: for automated `claude -p` calls, mates runtimes and check snapshots."""
import contextlib
import os
import shutil
import stat
import tempfile


class UnsafeDir(Exception):
    pass


def _private_base():
    """A folder outside the home folder that only we (and root) can write, up to `/`:
    the user runtime folder. A shared folder (/srv/mates/neutral is group emetcode, /tmp is
    everyone's) would let another account plant .claude/settings.json hooks or a CLAUDE.md
    that an unattended `claude -p` then runs or reads as Wai."""
    for base in (os.environ.get("XDG_RUNTIME_DIR"), f"/run/user/{os.getuid()}"):
        if base and os.path.isdir(base) and safe_ancestry(base):
            return base
    raise UnsafeDir("no private runtime folder (XDG_RUNTIME_DIR) for claude to run in")


def safe_ancestry(path) -> bool:
    """True if path and every folder above it is owned by us or root and writable by no one else."""
    p = os.path.realpath(path)
    while True:
        st = os.stat(p)
        if st.st_uid not in (0, os.getuid()) or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return False
        if p == "/":
            return True
        p = os.path.dirname(p)


def private_dir(path):
    """`path` as a mode-700 folder only we can write, up to `/` (made if missing). Raises
    UnsafeDir when it, or a folder above it, is writable by another account or not ours:
    then a file in it (a runtime, a check snapshot) could be another account's."""
    p = os.path.abspath(path)
    os.makedirs(p, mode=0o700, exist_ok=True)
    st = os.stat(p, follow_symlinks=False)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise UnsafeDir(f"{p} is not a folder of ours")
    os.chmod(p, 0o700)
    if not safe_ancestry(p):
        raise UnsafeDir(f"{p} or a folder above it is writable by another account")
    return p


def state_dir(*parts):
    """A private folder under ${XDG_STATE_HOME:-~/.local/state}/mates/ (see private_dir)."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return private_dir(os.path.join(base, "mates", *parts))


@contextlib.contextmanager
def private_cwd():
    """A fresh, empty mode-700 folder for one automated `claude -p` call, removed after."""
    d = tempfile.mkdtemp(prefix="mates-claude-", dir=_private_base())
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)
