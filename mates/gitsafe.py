"""Git on a Qwen worktree without trusting anything Qwen can write.

A Qwen worktree's `.git` file, its admin folder's `commondir`, a `.git` folder
planted under `.mates/`, or a tracked hooks folder could all steer a plain `git`
run there into running a command as Wai. `git_safe` never lets git discover the
repo: the git dirs come from the lead's checkout (trusted) and the read-only
`<common>/worktrees/*/gitdir` files, the index is a temp file unless the caller
names one, and hooks, fsmonitor, external diff and textconv are off.
"""
import contextlib
import os
import subprocess
import tempfile
from pathlib import Path

SAFE = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")
DIFF_SAFE = ("--no-ext-diff", "--no-textconv")
DIFF_COMMANDS = {"diff", "log", "show"}


class GitError(RuntimeError):
    pass


def worktree(root, n):
    return Path(root) / ".claude" / "worktrees" / f"mates-qwen-{n}"


def _argv(args):
    args = list(args)
    if args and args[0] in DIFF_COMMANDS:
        args[1:1] = DIFF_SAFE
    return ["git", *SAFE, *args]


def _env():
    """The caller's environment without GIT_* variables that could redirect git (the
    commit identity ones stay)."""
    return {k: v for k, v in os.environ.items()
            if not k.startswith("GIT_") or k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}


def _run(argv, cwd, env, check, input):
    r = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, input=input)
    if check and r.returncode != 0:
        raise GitError(r.stderr.strip() or f"{' '.join(argv)} exited {r.returncode}")
    return r


def git_lead(root, *args, check=True, input=None):
    """Git in the lead's own checkout `root` (normal discovery), with the same flags."""
    return _run(_argv(args), root, _env(), check, input)


def dirs(root, n):
    """(common git dir, admin folder, worktree) for Qwen worktree n. The admin folder is
    the entry under `<common>/worktrees/` whose `gitdir` file names `<worktree>/.git`."""
    common = Path(git_lead(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    wt = worktree(root, n)
    want = str(wt / ".git")
    for d in sorted((common / "worktrees").glob("*")):
        try:
            if (d / "gitdir").read_text().strip() == want:
                return common, d, wt
        except OSError:
            continue
    raise GitError(f"no git worktree entry for {wt}")


def git_safe(root, n, *args, index=None, check=True, input=None):
    """Run git on Qwen worktree n. `index` is the index file to use (default: a fresh
    temp file, deleted afterwards). Returns the CompletedProcess (text mode);
    `check=True` raises GitError(stderr) on a non-zero exit."""
    common, admin, wt = dirs(root, n)
    with contextlib.ExitStack() as stack:
        if index is None:
            tmp = stack.enter_context(tempfile.TemporaryDirectory(prefix="mates-index-"))
            index = Path(tmp) / "index"
        env = dict(_env(), GIT_DIR=str(admin), GIT_COMMON_DIR=str(common), GIT_WORK_TREE=str(wt),
                   GIT_INDEX_FILE=str(index))
        return _run(_argv(args), wt, env, check, input)
