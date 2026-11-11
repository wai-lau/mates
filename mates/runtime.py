"""Runtimes a check needs inside the sandbox, from two channels only.

1. The repo: a tracked `.mates/runtime.sh` read from the BASE commit (never
   the worker's tree), run as wai outside the sandbox with $MATES_RUNTIME set,
   cached under /srv/mates/runtimes/<repo>/<hash>/ (hash = script + the
   lockfiles named on its `# lockfiles:` line). Env additions go in
   $MATES_RUNTIME/env as KEY=VALUE lines.
2. Injection at launch: paths the lead names, copied read-only into the
   task's ctl/inject/, refused if they look like secrets.
"""
import hashlib
import os
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile

ROOT = pathlib.Path("/srv/mates/runtimes")
SCRIPT = ".mates/runtime.sh"
HOME = pathlib.Path.home()
SECRET_DIRS = [HOME / d for d in (".ssh", ".gnupg", ".secrets", ".claude", ".config/gh")]
SECRET_NAME = re.compile(r"(^\.credentials|^\.env($|\.)|\.pem$|\.key$)")
SECRET_TEXT = re.compile(r"(?i)(api[_-]?key|secret|token|passwd|password)\s*[:=]\s*\S{8,}"
                         r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|\bsk-[A-Za-z0-9]{20,}|\bgh[pousr]_[A-Za-z0-9]{30,}")


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


def runtime_hash(repo, base, script: bytes | None = None) -> str | None:
    """`script` overrides the repo's committed one (pilot: generated scripts, injected as the repo's would be)."""
    script = script if script is not None else _show(repo, base, SCRIPT)
    if script is None:
        return None
    h = hashlib.sha256(script)
    for lf in lockfiles(script):
        h.update(b"\0" + lf.encode() + b"\0" + (_show(repo, base, lf) or b""))
    return h.hexdigest()[:16]


def ensure_runtime(repo, base, root=ROOT, script: bytes | None = None) -> pathlib.Path | None:
    h = runtime_hash(repo, base, script)
    if h is None:
        return None
    dest = pathlib.Path(root) / pathlib.Path(repo).resolve().name / h
    if dest.is_dir():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".tmp-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    with tempfile.NamedTemporaryFile("wb", suffix=".sh", delete=False) as f:
        f.write(script if script is not None else _show(repo, base, SCRIPT))
    try:
        # Built at its final path's name so absolute paths baked in (venvs) resolve after rename:
        # build in tmp, but expose the final path via MATES_RUNTIME by symlinking it during the build.
        dest.symlink_to(tmp)
        r = subprocess.run(["bash", f.name], cwd=str(repo), env={**os.environ, "MATES_RUNTIME": str(dest)},
                           capture_output=True, text=True)
        dest.unlink()
        if r.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError(f"runtime build failed ({r.returncode}): {r.stderr[-2000:]}")
        for q in [tmp, *tmp.rglob("*")]:  # the worker's user shares the group: no group/other write
            if not q.is_symlink():
                q.chmod(q.stat().st_mode & ~(stat.S_IWGRP | stat.S_IWOTH))
        tmp.rename(dest)
    finally:
        os.unlink(f.name)
        if dest.is_symlink():
            dest.unlink()
    return dest


def runtime_env(d) -> list[str]:
    p = pathlib.Path(d) / "env"
    if not p.exists():
        return []
    return [l for l in p.read_text().splitlines() if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", l)]


def check_inject(path) -> None:
    p = pathlib.Path(os.path.expanduser(str(path))).absolute()
    for d in SECRET_DIRS:
        if p == d or d in p.parents:
            raise ValueError(f"refused: {p} is under {d}")
    files = [p] if not p.is_dir() else [f for f in p.rglob("*") if f.is_file()]
    for f in [p, *files]:
        if SECRET_NAME.search(f.name):
            raise ValueError(f"refused: {f.name} looks like a secret file")
    for f in files:
        if not f.exists():
            continue
        try:
            text = f.read_bytes()[:2_000_000].decode("utf-8", "ignore")
        except OSError:
            continue
        if SECRET_TEXT.search(text):
            raise ValueError(f"refused: {f} contains something that looks like a secret")


def _sha(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    for f in sorted([p] if p.is_file() else [x for x in p.rglob("*") if x.is_file()]):
        h.update(str(f.relative_to(p.parent if p.is_file() else p)).encode() + b"\0" + f.read_bytes())
    return h.hexdigest()


def _readonly(p: pathlib.Path):
    for f in [p, *(p.rglob("*") if p.is_dir() else [])]:
        mode = f.stat().st_mode
        f.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def stage_injects(task_dir, injects: list[str]) -> list[dict]:
    out = []
    base = pathlib.Path(task_dir) / "ctl" / "inject"
    base.mkdir(parents=True, exist_ok=True)
    for n, spec in enumerate(injects):
        src, _, dest = spec.partition(":")
        src_p = pathlib.Path(os.path.expanduser(src))
        check_inject(src_p)
        dest = dest or src_p.name
        staged = base / str(n)
        if src_p.is_dir():
            shutil.copytree(src_p, staged, symlinks=False)
        else:
            shutil.copy2(src_p, staged)
        _readonly(staged)
        out.append({"src": str(src_p), "dest": dest, "sha256": _sha(staged)})
    return out
