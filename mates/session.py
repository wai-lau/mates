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
from datetime import datetime
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
        _write(path, out)
    state["qwens"] = out["qwens"]


def update(root, fn):
    """Re-read the saved state under the exclusive lock, let `fn` change it in place, and
    write it back: for a command that worked from an older copy for a while and must
    change one entry without writing its stale copy of the others (the reaper)."""
    with _locked(root, fcntl.LOCK_EX) as path:
        state = _read(path)
        if state is None:
            return None
        fn(state)
        _write(path, state)
        return state


def drop(root, n):
    """Remove Qwen entry `n` from the saved state. Only `mates remove` calls this:
    `save` never drops an entry."""
    with _locked(root, fcntl.LOCK_EX) as path:
        old = _read(path)
        if old is not None:
            _write(path, dict(old, qwens=[q for q in old.get("qwens", []) if q["n"] != n]))


def _write(path, data):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


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
    for args in (("-t", pane_id, "@mates", f"{repo_key(root)}:{n}"), ("-u", "-t", pane_id, "@mates-failed")):
        r = _tmux("set-option", "-p", *args)
        if r.returncode != 0:  # an untagged live pane would later be marked closed
            raise RuntimeError(f"tmux set-option {' '.join(args)} failed: {r.stderr.strip()}")


def closed_by_wai(root, n):
    """Pane `n` is gone while this repo's lead pane is alive: Wai closed it. With the
    lead gone too (a reboot, the window closed) the pane is only away, and a restore
    must bring it back. A failed pane is still there (find_pane says "failed")."""
    return find_pane(root, n) is None and find_lead(root) is not None


def mark_closed(state, n):
    """Mark entry `n` closed if Wai closed its pane (closed_by_wai). Returns whether
    it was marked."""
    if not closed_by_wai(state["root"], n):
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
        # A compaction is stored as a finished reply, but OpenCode carries on after it.
        compaction = data.get("summary") is True or "compaction" in (data.get("mode"), data.get("agent"))
        if data.get("role") == "assistant" and (data.get("time") or {}).get("completed") \
                and data.get("finish") != "tool-calls" and not compaction:
            return "idle"
    quiet = last is None or now - last / 1000 > STALE_S
    if quiet and now - since > STALE_S:
        return "idle"
    return "busy"


def opencode_chats(repo, db, day):
    """All OpenCode messages in this repo's worktrees for `day` (ISO date).
    Returns a list of (message_id, created_ms, data_json, parts) for each message.
    Parts are a list of (part_id, part_data_json). Top-level sessions only."""
    import datetime as dt
    start = dt.datetime.fromisoformat(f"{day}T00:00:00+00:00")
    start_ms = int(start.timestamp() * 1000)
    end_ms = int((start + dt.timedelta(days=1)).timestamp() * 1000)  # the whole day, to midnight
    prefix = f"{repo}/.claude/worktrees/mates-qwen-"
    out = []
    with _ro(db) as con:
        sessions = con.execute(
            "SELECT id FROM session WHERE parent_id IS NULL AND directory LIKE ?",
            (prefix + "%",)).fetchall()
        for (sid,) in sessions:
            msgs = con.execute(
                "SELECT id, time_created, data FROM message WHERE session_id = ? AND "
                "time_created >= ? AND time_created < ? ORDER BY time_created, id",
                (sid, start_ms, end_ms)).fetchall()
            for mid, ms, data in msgs:
                parts = con.execute(
                    "SELECT id, data FROM part WHERE message_id = ? ORDER BY time_created, id",
                    (mid,)).fetchall()
                out.append((mid, ms, data, parts))
    return out


def opencode_usage(worktree, db, since):
    """(tokens, compactions, last_reply_s) summed over all top-level OpenCode sessions
    in `worktree` for assistant messages created at or after `since` (epoch s).
    tokens = sum of `tokens.input + tokens.output`; compactions = count of messages
    with `summary` true or `mode`/`agent` "compaction"; last_reply_s = newest
    `time.completed` (epoch s) or None."""
    tokens, compactions, last = 0, 0, None
    if not Path(db).exists():
        return tokens, compactions, last
    with _ro(db) as con:
        rows = con.execute("SELECT m.data FROM session s JOIN message m ON m.session_id = s.id "
                           "WHERE s.directory = ? AND s.parent_id IS NULL AND m.time_created >= ?",
                           (str(worktree), int(since * 1000))).fetchall()
    for (raw,) in rows:
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if data.get("role") != "assistant":
            continue
        t = data.get("tokens") or {}
        tokens += int(t.get("input") or 0) + int(t.get("output") or 0)
        if data.get("summary") or data.get("mode") == "compaction" or data.get("agent") == "compaction":
            compactions += 1
        done = (data.get("time") or {}).get("completed")
        if done and (last is None or done / 1000 > last):
            last = done / 1000
    return tokens, compactions, last


# --- Claude ---------------------------------------------------------------------------

LEAD_TOOLS = ("Bash", "Monitor")  # tools whose `command` input the lead runs `mates` through
MATES_CALL = re.compile(r"(?<![\w.-])mates\s+([a-z][a-z-]*)\s+(\d+)(?!\w)")


def _epoch(stamp):
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except (TypeError, ValueError):
        return None


def lead_tokens(transcript, n, since):
    """(lead_tokens, lead_cache_read) the lead spent on Qwen pane `n` after `since`
    (epoch s), from its transcript (JSONL): the assistant messages with a tool call
    whose command runs `mates <sub> n`. lead_tokens sums `input_tokens +
    cache_creation_input_tokens + output_tokens`, lead_cache_read sums
    `cache_read_input_tokens`. One API message is written as several entries (one per
    content block) with the same id: its usage counts once (largest value per field).
    Rough by design: other work the lead did while waiting is not counted."""
    usage, hits = {}, set()
    keys = ("input_tokens", "cache_creation_input_tokens", "output_tokens", "cache_read_input_tokens")
    try:
        fh = open(transcript, encoding="utf-8", errors="replace")
    except OSError:
        return 0, 0
    with fh:
        for line in fh:
            if '"assistant"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            ts = _epoch(d.get("timestamp")) if d.get("type") == "assistant" else None
            if ts is None or ts <= since:
                continue
            msg = d.get("message") or {}
            mid = msg.get("id") or d.get("uuid")
            u = msg.get("usage") or {}
            have = usage.setdefault(mid, dict.fromkeys(keys, 0))
            for k in keys:
                have[k] = max(have[k], int(u.get(k) or 0))
            for block in msg.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use" \
                        or block.get("name") not in LEAD_TOOLS:
                    continue
                cmd = (block.get("input") or {}).get("command")
                if isinstance(cmd, str) and any(int(m.group(2)) == n for m in MATES_CALL.finditer(cmd)):
                    hits.add(mid)
    tok = sum(usage[m]["input_tokens"] + usage[m]["cache_creation_input_tokens"] + usage[m]["output_tokens"]
              for m in hits)
    return tok, sum(usage[m]["cache_read_input_tokens"] for m in hits)


def transcript_path(root, state):
    """The lead's transcript file: the newest one titled as the state says, else the
    stored conversation; None if neither exists."""
    sid = claude_session(root, state.get("claude_title")) or state.get("claude_session")
    p = PROJECTS_DIR / repo_key(root) / f"{sid}.jsonl" if sid else None
    return p if p is not None and p.exists() else None

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
