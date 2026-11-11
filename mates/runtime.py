"""The runtime a Qwen pane and a check need inside the sandbox: a tracked
`.mates/runtime.sh` read from the BASE commit (never Qwen's tree), cached under
${XDG_STATE_HOME:-~/.local/state}/mates/runtimes/<repo>/<hash>/ (Wai-only
folders; hash = script + the lockfiles named on its `# lockfiles:` line). Env
additions go in $MATES_RUNTIME/env as KEY=VALUE lines.

The lead writes the script; the build runs it in its own bwrap sandbox (Wai,
2026-11-11: the sandboxed-install option, then "PII and stuff must never enter
the sandbox"). Inside: /usr, uv, the script, and ONLY the named lockfiles from the
base commit (never the checkout, so no uncommitted or ignored file); the runtime
folder at its final path; an empty home; no tokens. The one way out is
`qwen-relay egress`: HTTPS to the package registries on its list, nothing local.
"""
import fcntl
import hashlib
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

import fsutil

SCRIPT = ".mates/runtime.sh"
RELAY = pathlib.Path(__file__).resolve().parent.parent / "bin" / "qwen-relay"
PROXY = "127.0.0.1:3128"  # inside the build sandbox only
BUILD_TIMEOUT_S = 1800
ETC = ("nsswitch.conf", "ld.so.cache", "ld.so.conf", "ld.so.conf.d", "ssl", "ca-certificates", "alternatives",
       "localtime")


def _show(repo, base, path):
    """The blob at `base:path`, raw (no textconv), or None (missing, or not a file)."""
    r = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", f"{base}:{path}"], capture_output=True)
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


def _stage(repo, base, script, work):
    """Fill `work`: src/ with only the named lockfiles from `base`, the script, and a
    passwd/group naming just the build user (not the host's account list)."""
    src = work / "src"
    src.mkdir()
    for name in lockfiles(script):
        parts = pathlib.PurePosixPath(name).parts
        if name.startswith("/") or ".." in parts or not parts:
            raise RuntimeError(f"{SCRIPT}: lockfile {name!r} is not a path inside the repo")
        data = _show(repo, base, name)
        if data is None:
            raise RuntimeError(f"{SCRIPT}: lockfile {name} is not a file in commit {base}")
        (src / name).parent.mkdir(parents=True, exist_ok=True)
        (src / name).write_bytes(data)
    (work / "runtime.sh").write_bytes(script)
    (work / "passwd").write_text(f"build:x:{os.getuid()}:{os.getgid()}::/home/build:/bin/bash\n")
    (work / "group").write_text(f"build:x:{os.getgid()}:\n")


def _bwrap_argv(bwrap, work, dest, uv):
    a = [bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
         "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin",
         "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
         "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/home", "--dir", "/home/build",
         "--ro-bind", str(work / "passwd"), "/etc/passwd", "--ro-bind", str(work / "group"), "/etc/group"]
    for f in ETC:
        if os.path.exists(f"/etc/{f}"):
            a += ["--ro-bind", f"/etc/{f}", f"/etc/{f}"]
    a += ["--bind", str(work / "src"), "/work/src",
          "--ro-bind", str(work / "runtime.sh"), "/run/build/runtime.sh",
          "--ro-bind", str(work / "egress.sock"), "/run/build/egress.sock",
          "--ro-bind", str(RELAY), "/opt/mates/qwen-relay"]
    if uv:
        a += ["--ro-bind", uv, "/opt/mates/bin/uv"]
    a += ["--bind", str(dest), str(dest)]  # last: under the /home or /tmp tmpfs, at its final path
    proxy = f"http://{PROXY}"
    env = {"HOME": "/home/build", "PATH": "/opt/mates/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
           "MATES_RUNTIME": str(dest), "UV_PYTHON_DOWNLOADS": "never", "UV_LINK_MODE": "copy",
           "HTTPS_PROXY": proxy, "https_proxy": proxy, "HTTP_PROXY": proxy, "http_proxy": proxy}
    for k, v in env.items():
        a += ["--setenv", k, v]
    port = PROXY.rsplit(":", 1)[1]
    inner = (f"python3 /opt/mates/qwen-relay in --sock /run/build/egress.sock --listen {PROXY} 2>/dev/null & "
             f"for _ in $(seq 50); do (exec 3<>/dev/tcp/127.0.0.1/{port}) 2>/dev/null && break; sleep 0.1; done; "
             "cd /work/src && exec bash /run/build/runtime.sh")
    return a + ["--chdir", "/work/src", "--", "/bin/bash", "-c", inner]


def _build(repo, base, script, dest, log):
    """Run the script in the build sandbox; its output and every egress line go to `log`."""
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("bwrap not installed: refusing to build the runtime outside a sandbox")
    uv = shutil.which("uv")
    work = pathlib.Path(tempfile.mkdtemp(prefix=f".{dest.name}.build.", dir=dest.parent))
    try:
        _stage(repo, base, script, work)
        with open(log, "w") as lf:
            proxy = subprocess.Popen([sys.executable, "-I", str(RELAY), "egress", "--sock", str(work / "egress.sock")],
                                     stdin=subprocess.DEVNULL, stdout=lf, stderr=lf)
            try:
                for _ in range(100):
                    if (work / "egress.sock").exists() or proxy.poll() is not None:
                        break
                    time.sleep(0.05)
                if not (work / "egress.sock").exists():
                    raise RuntimeError(f"the egress proxy did not start (log: {log})")
                try:
                    r = subprocess.run(_bwrap_argv(bwrap, work, dest, os.path.realpath(uv) if uv else None),
                                       cwd="/", stdin=subprocess.DEVNULL, stdout=lf, stderr=lf, timeout=BUILD_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    raise RuntimeError(f"runtime build ran over {BUILD_TIMEOUT_S}s (log: {log})")
            finally:
                proxy.kill()
                proxy.wait()
        if r.returncode != 0:
            tail = pathlib.Path(log).read_text(errors="replace")[-2000:]
            raise RuntimeError(f"runtime build failed ({r.returncode}; log: {log}):\n{tail}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


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
        try:
            _build(repo, base, _show(repo, base, SCRIPT), dest, parent / f"{h}.log")
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)
            raise
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
