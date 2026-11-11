"""Removing task dirs that hold files the worker (user `claude`) created owner-only, and
the private working folder for automated `claude -p` calls."""
import contextlib
import os
import shutil
import stat
import subprocess
import tempfile


def open_up(path) -> None:
    """Make everything under path group-accessible: as wai for our files, as claude for the worker's."""
    subprocess.run(["chmod", "-R", "u+rwX", str(path)], capture_output=True)
    subprocess.run(["sudo", "-n", "-u", "claude", "chmod", "-R", "g+rwX", str(path)], capture_output=True)


def rmtree(path) -> None:
    if not os.path.lexists(path):
        return
    open_up(path)
    shutil.rmtree(path, ignore_errors=True)


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


@contextlib.contextmanager
def private_cwd():
    """A fresh, empty mode-700 folder for one automated `claude -p` call, removed after."""
    d = tempfile.mkdtemp(prefix="mates-claude-", dir=_private_base())
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)
