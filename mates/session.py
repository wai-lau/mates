"""Per-repo mates state, and finding what that state names: tmux panes by tag,
Qwen worktrees, OpenCode sessions and the lead's Claude transcript.

State lives in `~/.claude/mates/sessions/<repo-key>.json`, written atomically
under `flock` on `<repo-key>.lock`. A stored tmux pane id is never trusted:
panes are found by their `@mates=<repo-key>:<n>` option.
"""
import contextlib
import fcntl
import glob
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

import layout

PARALLEL_DEFAULT = False  # docs/mates-probe.md: the two-pane test was not passed
STATE_DIR = Path.home() / ".claude" / "mates" / "sessions"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
EXCLUDES = (".mates/", "/.claude/worktrees/")
STALE_S = 300  # no OpenCode update for this long: a crashed reply stops counting as busy


def repo_key(root):
    """The `~/.claude/projects` rule: every character outside [A-Za-z0-9] becomes `-`."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(root))


def new_state(root):
    return {"root": str(root), "claude_session": None, "claude_title": f"mates {Path(root).name}",
            "parallel": PARALLEL_DEFAULT, "widths": [], "qwens": []}


# --- state file ---------------------------------------------------------------

@contextlib.contextmanager
def _locked(root, mode):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / f"{repo_key(root)}.lock", "a") as lk:
        fcntl.flock(lk, mode)
        yield STATE_DIR / f"{repo_key(root)}.json"


def _read(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def load(root):
    with _locked(root, fcntl.LOCK_SH) as path:
        return _read(path)


def save(root, state):
    """Write `state` atomically. Never drops entries: a Qwen entry on disk that
    `state` lacks is kept (a pane that is gone is marked closed, not removed),
    and `state["qwens"]` is updated to the merged list."""
    with _locked(root, fcntl.LOCK_EX) as path:
        old = _read(path) or {}
        have = {q["n"] for q in state.get("qwens", [])}
        kept = [q for q in old.get("qwens", []) if q["n"] not in have]
        out = dict(state, qwens=sorted(state.get("qwens", []) + kept, key=lambda q: q["n"]))
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(out, f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
    state["qwens"] = out["qwens"]


# --- panes by tag ---------------------------------------------------------------

def _tmux(*args):
    return subprocess.run([layout.TMUX, *args], capture_output=True, text=True)


def _tagged(tag):
    """(pane_id, failed) for the first pane in any session whose @mates is `tag`."""
    r = _tmux("list-panes", "-a", "-F", "#{pane_id}\t#{@mates}\t#{@mates-failed}")
    if r.returncode != 0:  # no tmux server: no panes
        return None
    for line in r.stdout.splitlines():
        pid, _, rest = line.partition("\t")
        name, _, failed = rest.partition("\t")
        if name == tag:
            return pid, failed == "1"
    return None


def find_pane(root, n):
    """(pane_id, "ok"|"failed") for Qwen pane `n`, or None when it is gone."""
    hit = _tagged(f"{repo_key(root)}:{n}")
    return None if hit is None else (hit[0], "failed" if hit[1] else "ok")


def find_lead(root):
    hit = _tagged(f"{repo_key(root)}:lead")
    return None if hit is None else hit[0]


def tag_pane(pane_id, root, n):
    """Tag a pane as this repo's pane `n` ("lead" for Claude) and clear any failed mark."""
    _tmux("set-option", "-p", "-t", pane_id, "@mates", f"{repo_key(root)}:{n}")
    _tmux("set-option", "-p", "-u", "-t", pane_id, "@mates-failed")


def mark_closed(state, n):
    """Mark entry `n` closed if its pane is gone. A failed pane is still there
    (find_pane says "failed"), so it stays open. Returns whether it was marked."""
    if find_pane(state["root"], n) is not None:
        return False
    for q in state["qwens"]:
        if q["n"] == n:
            q["closed"] = True
            return True
    return False


# --- worktrees --------------------------------------------------------------------

def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def _add_excludes(root):
    path = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude").strip())
    have = path.read_text().splitlines() if path.exists() else []
    missing = [line for line in EXCLUDES if line not in have]
    if missing:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            if have and not path.read_text().endswith("\n"):
                f.write("\n")
            f.write("".join(line + "\n" for line in missing))


def ensure_worktree(root, n):
    """Create or reuse `.claude/worktrees/mates-qwen-<n>` on `mates/qwen-<n>`
    (a new branch starts at the lead's HEAD). Returns (path, branch)."""
    root = Path(root).resolve()
    path, branch = root / ".claude" / "worktrees" / f"mates-qwen-{n}", f"mates/qwen-{n}"
    _add_excludes(root)
    listed = _git(root, "worktree", "list", "--porcelain").splitlines()
    if f"worktree {path}" in listed and path.exists():
        return path, branch
    _git(root, "worktree", "prune")
    if subprocess.run(["git", "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
                      cwd=root, capture_output=True).returncode == 0:
        _git(root, "worktree", "add", "-q", str(path), branch)
    else:
        _git(root, "worktree", "add", "-q", "-b", branch, str(path), "HEAD")
    return path, branch


# --- OpenCode ----------------------------------------------------------------------

def _qwen():
    return os.environ.get("MATES_QWEN") or str(Path(__file__).resolve().parent.parent / "bin" / "qwen")


def opencode_db(root):
    out = subprocess.run([_qwen(), "opencode-dir"], cwd=root, check=True, capture_output=True, text=True).stdout
    return Path(out.strip()) / "opencode.db"


@contextlib.contextmanager
def _ro(db):
    con = sqlite3.connect(Path(db).absolute().as_uri() + "?mode=ro", uri=True)
    try:
        yield con
    finally:
        con.close()


def opencode_session(worktree, db):
    """The newest top-level OpenCode session run in `worktree`, or None."""
    if not Path(db).exists():
        return None
    with _ro(db) as con:
        row = con.execute("SELECT id FROM session WHERE directory = ? AND parent_id IS NULL "
                          "ORDER BY time_updated DESC LIMIT 1", (str(worktree),)).fetchone()
    return row[0] if row else None


def opencode_state(session, db, since):
    """ "idle" or "busy" for an OpenCode session sent a message at `since` (epoch s).
    The one busy rule (send, check and wait all use it):
    - idle if the newest message came after `since` and is a finished assistant
      reply (`time.completed` set, and not a step that ends in tool calls);
    - idle if nothing in the session was updated for STALE_S and `since` is
      older than that, so a crashed OpenCode stops counting as busy;
    - otherwise busy (right after a paste, before any reply, this is busy)."""
    now = time.time()
    newest, last = None, None
    if Path(db).exists():
        with _ro(db) as con:
            newest = con.execute("SELECT time_created, data FROM message WHERE session_id = ? "
                                 "ORDER BY time_created DESC, id DESC LIMIT 1", (session,)).fetchone()
            last = con.execute("SELECT max(t) FROM (SELECT max(time_updated) t FROM message WHERE session_id = ?1 "
                               "UNION ALL SELECT max(time_updated) FROM part WHERE session_id = ?1)",
                               (session,)).fetchone()[0]
    if newest and newest[0] > since * 1000:
        data = json.loads(newest[1])
        if data.get("role") == "assistant" and (data.get("time") or {}).get("completed") \
                and data.get("finish") != "tool-calls":
            return "idle"
    quiet = last is None or now - last / 1000 > STALE_S
    if quiet and now - since > STALE_S:
        return "idle"
    return "busy"


# --- Claude ---------------------------------------------------------------------------

def claude_session(root, title):
    """The newest transcript under ~/.claude/projects/<repo-key>/ whose last
    custom-title is `title` (follows /clear and resumed forks), or None."""
    files = glob.glob(str(PROJECTS_DIR / repo_key(root) / "*.jsonl"))
    for f in sorted(files, key=os.path.getmtime, reverse=True):
        last = None
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"custom-title"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("type") == "custom-title":
                        last = d.get("customTitle") or ""
        except OSError:
            continue
        if last == title:
            return Path(f).stem
    return None
