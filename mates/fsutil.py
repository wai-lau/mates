"""Removing task dirs that hold files the worker (user `claude`) created owner-only."""
import os
import shutil
import subprocess


def open_up(path) -> None:
    """Make everything under path group-accessible: as wai for our files, as claude for the worker's."""
    subprocess.run(["chmod", "-R", "u+rwX", str(path)], capture_output=True)
    subprocess.run(["sudo", "-n", "-u", "claude", "chmod", "-R", "g+rwX", str(path)], capture_output=True)


def rmtree(path) -> None:
    if not os.path.lexists(path):
        return
    open_up(path)
    shutil.rmtree(path, ignore_errors=True)
