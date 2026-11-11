"""The runtime a Qwen pane and a check need inside the sandbox: a tracked
`.mates/runtime.sh` read from the BASE commit (never Qwen's tree), run as Wai
outside the sandbox with $MATES_RUNTIME set, cached under
${XDG_STATE_HOME:-~/.local/state}/mates/runtimes/<repo>/<hash>/ (Wai-only
folders; hash = script + the lockfiles named on its `# lockfiles:` line). Env
additions go in $MATES_RUNTIME/env as KEY=VALUE lines.
"""
import fcntl
import hashlib
import os
import pathlib
import re
import shutil
import stat
import subprocess

import fsutil

SCRIPT = ".mates/runtime.sh"


def _show(repo, base, path):
    r = subprocess.run(["git", "-C", str(repo), "show", f"{base}:{path}"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def lockfiles(script: bytes) -> list[str]:
    """The names on the script's `# lockfiles:` line."""
    m = re.search(rb"^# lockfiles:(.*)$", script, re.M)
    return [lf.decode() for lf in m.group(1).split()] if m else []


def runtime_files(repo, base) -> list[str]:
    """Every file `runtime_hash` covers at `base`: the script and its lockfiles ([] without a script)."""
    script = _show(repo, base, SCRIPT)
    return [] if script is None else [SCRIPT, *lockfiles(script)]


def runtime_hash(repo, base) -> str | None:
    script = _show(repo, base, SCRIPT)
    if script is None:
        return None
    h = hashlib.sha256(script)
    for lf in lockfiles(script):
        h.update(b"\0" + lf.encode() + b"\0" + (_show(repo, base, lf) or b""))
    return h.hexdigest()[:16]


def ensure_runtime(repo, base, root=None) -> pathlib.Path | None:
    """The runtime for `repo` at `base`, built once. `root` (default
    ${XDG_STATE_HOME:-~/.local/state}/mates/runtimes) and the per-repo folder must be
    folders only Wai can write, up to `/` (fsutil.private_dir): a runtime in a folder
    another account can write (the old /srv/mates/runtimes was group emetcode) could be
    swapped, and its env PATH would then run that account's programs in every check.
    Built straight at its final path (no symlink step); `<hash>.ok` marks it complete."""
    h = runtime_hash(repo, base)
    if h is None:
        return None
    root = fsutil.state_dir("runtimes") if root is None else fsutil.private_dir(root)
    parent = pathlib.Path(fsutil.private_dir(pathlib.Path(root) / pathlib.Path(repo).resolve().name))
    dest, ok = parent / h, parent / f"{h}.ok"
    with open(parent / f"{h}.lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)  # two launches at once build it once
        st = os.lstat(dest) if os.path.lexists(dest) else None
        if st is not None and stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and ok.is_file():
            return dest
        ok.unlink(missing_ok=True)
        if st is not None:  # unfinished, or not a plain folder of ours: never trusted
            if stat.S_ISDIR(st.st_mode):
                shutil.rmtree(dest)
            else:
                dest.unlink()
        dest.mkdir(mode=0o700)
        sh = parent / f"{h}.sh"
        sh.write_bytes(_show(repo, base, SCRIPT))
        try:
            r = subprocess.run(["bash", str(sh)], cwd=str(repo), env={**os.environ, "MATES_RUNTIME": str(dest)},
                               capture_output=True, text=True)
        finally:
            sh.unlink()
        if r.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            raise RuntimeError(f"runtime build failed ({r.returncode}): {r.stderr[-2000:]}")
        for q in [dest, *dest.rglob("*")]:  # read-only to everyone else
            if not q.is_symlink():
                q.chmod(q.stat().st_mode & ~(stat.S_IWGRP | stat.S_IWOTH))
        ok.touch()
    return dest


def runtime_env(d) -> list[str]:
    p = pathlib.Path(d) / "env"
    if not p.exists():
        return []
    return [l for l in p.read_text().splitlines() if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", l)]
