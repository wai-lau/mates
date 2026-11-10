"""Pilot task set: candidate commits from Wai's repos, validated in the sandbox.

A candidate is a non-merge commit that changes 1-3 non-test source files and
at least one (non-browser) test file. It becomes a task only if its check is
red on the parent commit and green on the commit itself, inside the sandbox
with the repo's runtime.
"""
import pathlib
import re
import shutil
import subprocess

import runtime

SRC = re.compile(r"\.(py|js|mjs|ts)$")
TEST = re.compile(r"(^|/)tests?/.*test_[^/]*\.py$|(^|/)test_[^/]*\.py$")
BROWSER = re.compile(r"_browser\.py$")

UV_SCRIPT = """# lockfiles: uv.lock pyproject.toml
set -euo pipefail
export UV_CACHE_DIR=$MATES_RUNTIME/uv-cache UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=$MATES_RUNTIME/venv
~/.local/bin/uv sync --frozen --all-extras --quiet
rm -rf "$UV_CACHE_DIR"
echo "PATH=$MATES_RUNTIME/venv/bin:/usr/bin:/bin" > "$MATES_RUNTIME/env"
echo "VIRTUAL_ENV=$MATES_RUNTIME/venv" >> "$MATES_RUNTIME/env"
"""
REQ_SCRIPT = """# lockfiles: {req}
set -euo pipefail
python3 -m venv "$MATES_RUNTIME/venv"
"$MATES_RUNTIME/venv/bin/pip" install -q -r {req}
echo "PATH=$MATES_RUNTIME/venv/bin:/usr/bin:/bin" > "$MATES_RUNTIME/env"
echo "VIRTUAL_ENV=$MATES_RUNTIME/venv" >> "$MATES_RUNTIME/env"
"""


def _git(repo, *a, check=True):
    return subprocess.run(["git", "-C", str(repo), *a], check=check, capture_output=True, text=True).stdout.strip()


def candidates(repo, since="6 months ago", limit=200) -> list[dict]:
    out = []
    log = _git(repo, "log", "--no-merges", f"--since={since}", f"-n{limit}", "--format=%x00%H%x01%P%x01%s",
               "--name-only")
    for block in log.split("\x00")[1:]:
        head, *files = block.strip("\n").split("\n")
        commit, parents, msg = head.split("\x01")
        if len(parents.split()) != 1:
            continue
        files = [f for f in files if f]
        tests = [f for f in files if TEST.search(f)]
        src = [f for f in files if SRC.search(f) and not TEST.search(f)]
        if any(BROWSER.search(f) for f in tests) or not tests or not 1 <= len(src) <= 3:
            continue
        out.append({"commit": commit, "parent": parents, "msg": msg, "src_files": src, "test_files": tests})
    return out


def runtime_script_for(repo) -> str | None:
    r = pathlib.Path(repo)
    if (r / "uv.lock").exists():
        return UV_SCRIPT
    for req in ("requirements-dev.txt", "requirements.txt"):
        if (r / req).exists():
            return REQ_SCRIPT.format(req=req)
    return None


def check_for(test_files: list[str]) -> str:
    return "python3 -m pytest -q -p no:cacheprovider " + " ".join(test_files)


def _run_check(repo, commit, check, sandbox, td, rt):
    if td.exists():
        _git(repo, "worktree", "remove", "--force", str(td / "wt"), check=False)
        shutil.rmtree(td, ignore_errors=True)
    for d in ("ctl", "state", "sock"):
        (td / d).mkdir(parents=True)
    _git(repo, "worktree", "add", "--detach", str(td / "wt"), commit)
    (td / "wt" / ".mates").mkdir(exist_ok=True)
    (td / "ctl" / "check.sh").write_text("#!/usr/bin/env bash\n" + check + "\n")
    (td / "ctl" / "check.sh").chmod(0o644)
    args = [sandbox, "--task-dir", str(td)] + (["--runtime", str(rt)] if rt else []) + ["--", "bash", ".mates/check.sh"]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=900)
        return r.returncode, (r.stdout + r.stderr)[-1500:]
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    finally:
        _git(repo, "worktree", "remove", "--force", str(td / "wt"), check=False)
        shutil.rmtree(td, ignore_errors=True)


def make_base(repo, cand: dict, scratch: pathlib.Path) -> str:
    """The task's starting point: the parent's source with the target commit's tests."""
    wt = scratch / "mkbase"
    if wt.exists():
        _git(repo, "worktree", "remove", "--force", str(wt), check=False)
    _git(repo, "worktree", "add", "-q", "--detach", str(wt), cand["parent"])
    try:
        _git(wt, "checkout", cand["commit"], "--", *cand["test_files"])
        _git(wt, "-c", "user.name=mates-pilot", "-c", "user.email=mates@localhost", "-c", "commit.gpgsign=false",
             "commit", "-q", "--no-verify", "-m", f"pilot base for {cand['commit'][:10]}")
        return _git(wt, "rev-parse", "HEAD")
    finally:
        _git(repo, "worktree", "remove", "--force", str(wt), check=False)


def validate(repo, cand: dict, check: str, *, sandbox, tasks_root, script: str | None = None,
             runtime_root=runtime.ROOT) -> tuple[bool, str, str | None]:
    rt = None
    if script:
        try:
            rt = runtime.ensure_runtime(repo, cand["parent"], root=runtime_root, script=script.encode())
        except RuntimeError as e:
            return False, f"runtime build failed: {str(e)[:300]}", None
    tr = pathlib.Path(tasks_root)
    tr.mkdir(parents=True, exist_ok=True)
    base = make_base(repo, cand, tr)
    rc, out = _run_check(repo, base, check, sandbox, tr / "validate-base", rt)
    last = (out.strip().splitlines() or [""])[-1][:200]
    if rc in (126, 127) or re.search(r"command not found|No module named pytest", out):
        return False, "runtime-missing: " + last, None
    if rc == 0:
        return False, "green on base", None
    rc, out = _run_check(repo, cand["commit"], check, sandbox, tr / "validate-target", rt)
    if rc != 0:
        return False, "red on target: " + (out.strip().splitlines() or [""])[-1][:200], None
    return True, "ok", base
