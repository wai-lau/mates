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
    # QWEN_ROOT names the repo: run without it, bin/qwen takes a hand launch, which may mount
    # only a project under ~/src
    out = subprocess.run([_qwen(), "opencode-dir"], cwd=root, check=True, capture_output=True, text=True,
                         env={**os.environ, "QWEN_ROOT": str(root)}).stdout
    return Path(out.strip()) / "opencode.db"


# The database file is bound read-write into Qwen's sandbox, so Qwen controls what mates reads:
# a recursive view in place of `message` would make a query run forever, and a corrupt or
# unreadable file raises. Every connection therefore gets a time budget (a progress handler
# aborts the running query with sqlite3.OperationalError once it is spent), is read-only and
# query_only, and must hold real tables `session`, `message` and `part`. Any failure is a
# sqlite3.Error; each caller has its own can't-read answer.
DB_BUDGET_S = 5.0
CHATS_BUDGET_S = 30.0  # the retro reads a whole day of every session
DB_TABLES = ("session", "message", "part")


@contextlib.contextmanager
def _ro(db, budget=None, need=DB_TABLES):
    deadline = time.monotonic() + (DB_BUDGET_S if budget is None else budget)
    con = sqlite3.connect(Path(db).absolute().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
        con.execute("PRAGMA query_only = ON")
        real = {n for (n,) in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND sql NOT LIKE 'CREATE VIRTUAL%'")}
        if not set(need) <= real:
            raise sqlite3.DatabaseError(f"the OpenCode database lacks a real {'/'.join(need)} table")
        yield con
    finally:
        con.close()


def _data(raw):
    """A message's data as a dict, or None when it is not valid JSON or not an object."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _completed(data):
    """`time.completed` of a message (epoch ms), or None; a `time` that is no object is None."""
    t = data.get("time")
    return t.get("completed") if isinstance(t, dict) else None


def reply_finished(data):
    """The one rule for "is this assistant message a finished reply" (state, mid-reply and
    usage all use it): completed (`time.completed` set), and not a step that ends in tool
    calls, unless that step stopped on an error (an Escape abort ends the reply)."""
    if data.get("role") != "assistant" or not _completed(data):
        return False
    return data.get("finish") != "tool-calls" or bool(data.get("error"))


def reply_compaction(data):
    """True for an assistant message that is a compaction (`summary` set, or `mode`/`agent`
    "compaction"): stored as a finished reply, but OpenCode carries on after an automatic one."""
    return data.get("role") == "assistant" and bool(
        data.get("summary") or "compaction" in (data.get("mode"), data.get("agent")))


def _last_write(con, session):
    """When OpenCode last wrote a message or part of `session` (epoch ms), or None."""
    return con.execute("SELECT max(t) FROM (SELECT max(time_updated) t FROM message WHERE session_id = ? "
                       "UNION ALL SELECT max(time_updated) FROM part WHERE session_id = ?)",
                       (session, session)).fetchone()[0]


def last_activity(worktree, db):
    """When OpenCode last wrote a message or part in any session run in `worktree` (epoch s),
    or None with none or when the database cannot be read."""
    if not Path(db).exists():
        return None
    try:
        with _ro(db) as con:
            row = con.execute(
                "SELECT max(t) FROM (SELECT max(time_updated) t FROM message WHERE session_id IN "
                "(SELECT id FROM session WHERE directory = ?) UNION ALL SELECT max(time_updated) FROM part "
                "WHERE session_id IN (SELECT id FROM session WHERE directory = ?))",
                (str(worktree), str(worktree))).fetchone()
    except sqlite3.Error:
        return None
    return row[0] / 1000 if row and row[0] else None


def opencode_session(worktree, db):
    """The newest top-level OpenCode session run in `worktree`, or None."""
    if not Path(db).exists():
        return None
    try:
        with _ro(db, need=("session",)) as con:
            row = con.execute("SELECT id FROM session WHERE directory = ? AND parent_id IS NULL "
                              "ORDER BY time_updated DESC LIMIT 1", (str(worktree),)).fetchone()
    except sqlite3.Error:
        return None  # can't read: as if there were no session
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
        try:
            with _ro(db) as con:
                newest = con.execute("SELECT time_created, data FROM message WHERE session_id = ? "
                                     "ORDER BY time_created DESC, id DESC LIMIT 1", (session,)).fetchone()
                last = _last_write(con, session)
        except sqlite3.Error:
            newest, last = None, None  # can't read: as with no database, so only the clock decides
    if newest and newest[0] > since * 1000:
        data = _data(newest[1])
        # A compaction is stored as a finished reply, but OpenCode carries on after it.
        if data and reply_finished(data) and not reply_compaction(data):
            return "idle"
    quiet = last is None or now - last / 1000 > STALE_S
    if quiet and now - since > STALE_S:
        return "idle"
    return "busy"


# After an automatic compaction OpenCode sends itself two user messages: an empty one (only a
# "compaction" part) that asks for the summary, then this line as a synthetic text part.
OPENCODE_CONTINUE = "Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed."
PEOPLE_PARTS = ("file", "agent", "subtask")  # parts only a person's message has (attachments, @mentions)


def _opencode_own(parts):
    """True for a user message OpenCode wrote itself: no text (its compaction request), or
    exactly its continue line. A part that will not parse, or an attachment, is a person's."""
    try:
        parts = [json.loads(p) for p in parts]
    except ValueError:
        return False
    if not all(isinstance(p, dict) for p in parts) or any(p.get("type") in PEOPLE_PARTS for p in parts):
        return False
    text = "".join(str(p.get("text") or "") for p in parts if p.get("type") == "text").strip()
    return text in ("", OPENCODE_CONTINUE)


def auto_continued(worktree, db, since):
    """True when there are user messages in `worktree`'s top-level OpenCode sessions from
    `since` (epoch s) on, and OpenCode sent every one itself (_opencode_own): Qwen is
    replying only because OpenCode continued it after compacting. A message from a lead or
    from Wai makes it False. Message text is only compared with OpenCode's line, never
    returned. Subagent sessions do not count: their prompts come from Qwen."""
    if not Path(db).exists():
        return False
    own = []
    try:
        with _ro(db) as con:
            rows = con.execute("SELECT m.id, m.data FROM session s JOIN message m ON m.session_id = s.id "
                               "WHERE s.directory = ? AND s.parent_id IS NULL AND m.time_created >= ?",
                               (str(worktree), int(since * 1000))).fetchall()
            for mid, raw in rows:
                data = _data(raw)
                if data is None:
                    return False
                if data.get("role") == "user":
                    parts = [p for (p,) in con.execute("SELECT data FROM part WHERE message_id = ?", (mid,))]
                    own.append(_opencode_own(parts))
    except sqlite3.Error:
        return False  # can't read: never claim OpenCode alone spoke
    return bool(own) and all(own)


def opencode_mid_reply(session, db):
    """When OpenCode last wrote to `session` (epoch s) if its newest assistant message is
    mid-reply, else None. Mid-reply: not completed (streaming, or waiting for the model), or
    a step that ended in tool calls, so more steps come, unless it stopped on an error (an
    Escape aborts with one). A compaction summary is not mid-reply: after an automatic one
    OpenCode starts a new reply, after a /compact by hand none comes."""
    if session is None or not Path(db).exists():
        return None
    try:
        with _ro(db) as con:
            newest = None
            for (raw,) in con.execute("SELECT data FROM message WHERE session_id = ? "
                                      "ORDER BY time_created DESC, id DESC", (session,)):
                data = _data(raw)
                if data and data.get("role") == "assistant":
                    newest = data
                    break
            if newest is None or reply_finished(newest):
                return None
            last = _last_write(con, session)
    except sqlite3.Error:
        return None  # can't read: not mid-reply (the screen decides busy)
    return last / 1000 if last else None


def opencode_usage(worktree, db, since):
    """(tokens, compactions, last_reply_s) summed over all top-level OpenCode sessions
    in `worktree` for assistant messages created at or after `since` (epoch s).
    tokens = sum of `tokens.input + tokens.output`; compactions = count of messages
    with `summary` true or `mode`/`agent` "compaction"; last_reply_s = newest
    `time.completed` (epoch s) or None."""
    tokens, compactions, last = 0, 0, None
    if not Path(db).exists():
        return tokens, compactions, last
    with _ro(db) as con:  # a failure is sqlite3.Error: the callers say what they do about it
        rows = con.execute("SELECT m.data FROM session s JOIN message m ON m.session_id = s.id "
                           "WHERE s.directory = ? AND s.parent_id IS NULL AND m.time_created >= ?",
                           (str(worktree), int(since * 1000))).fetchall()
    for (raw,) in rows:
        data = _data(raw)
        if data is None or data.get("role") != "assistant":
            continue
        try:
            t = data.get("tokens") or {}
            tokens += int(t.get("input") or 0) + int(t.get("output") or 0)
        except (ValueError, TypeError, AttributeError):
            pass  # a token count Qwen mangled is not a count
        if reply_compaction(data):
            compactions += 1
        done = _completed(data)
        if isinstance(done, (int, float)) and done and (last is None or done / 1000 > last):
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


def lead_usage(transcript, n, since):
    """What the lead spent after `since` (epoch s), from its transcript (JSONL), as a dict of
    `input`, `cache_write`, `output` and `cache_read` token sums; None when the transcript
    cannot be read. With `n`, only the assistant messages with a tool call whose command runs
    `mates <sub> n` count; with n None, every assistant message does. One API message is
    written as several entries (one per content block) with the same id: its usage counts once
    (largest value per field). Rough by design: other work the lead did while waiting is not
    counted for a pane."""
    usage, hits = {}, set()
    keys = ("input_tokens", "cache_creation_input_tokens", "output_tokens", "cache_read_input_tokens")
    try:
        fh = open(transcript, encoding="utf-8", errors="replace")  # noqa: SIM115 (the open can fail; the with follows)
    except OSError:
        return None
    with fh:
        for line in fh:
            if '"assistant"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            ts = _epoch(d.get("timestamp")) if isinstance(d, dict) and d.get("type") == "assistant" else None
            if ts is None or ts <= since:
                continue
            msg = d.get("message") or {}
            mid = msg.get("id") or d.get("uuid")
            u = msg.get("usage") or {}
            have = usage.setdefault(mid, dict.fromkeys(keys, 0))
            for k in keys:
                have[k] = max(have[k], int(u.get(k) or 0))
            if n is None:
                hits.add(mid)
            for block in msg.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use" \
                        or block.get("name") not in LEAD_TOOLS:
                    continue
                cmd = (block.get("input") or {}).get("command")
                if isinstance(cmd, str) and any(int(m.group(2)) == n for m in MATES_CALL.finditer(cmd)):
                    hits.add(mid)
    return {"input": sum(usage[m]["input_tokens"] for m in hits),
            "cache_write": sum(usage[m]["cache_creation_input_tokens"] for m in hits),
            "output": sum(usage[m]["output_tokens"] for m in hits),
            "cache_read": sum(usage[m]["cache_read_input_tokens"] for m in hits)}


def lead_tokens(transcript, n, since):
    """(lead_tokens, lead_cache_read): lead_usage's input + cache_write + output, and its
    cache_read; (0, 0) when the transcript cannot be read."""
    u = lead_usage(transcript, n, since)
    return (u["input"] + u["cache_write"] + u["output"], u["cache_read"]) if u else (0, 0)


def lead_cost_fields(transcript, n, since):
    """The log fields for the lead's spend (see lead_usage), all None without a transcript."""
    u = lead_usage(transcript, n, since) if transcript else None
    if u is None:
        return dict.fromkeys(("lead_tokens", "lead_cache_read", "lead_input", "lead_cache_write", "lead_output"))
    return {"lead_tokens": u["input"] + u["cache_write"] + u["output"], "lead_cache_read": u["cache_read"],
            "lead_input": u["input"], "lead_cache_write": u["cache_write"], "lead_output": u["output"]}


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
