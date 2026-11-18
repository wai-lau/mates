"""The mates audit log: one validated JSON line per task."""
import calendar
import json
import os
import time
from pathlib import Path

FIELDS = ("ts", "repo", "task_id", "summary", "route", "reason", "check", "check_sha", "red_on_base",
          "flags", "attempts", "outcome", "answered_by", "worker_wall_s", "worker_tokens", "compactions",
          "verdict", "lead_tokens", "questions", "pane", "runtime", "restarted", "event", "n",
          "send_check",  # the command sent with `mates send --check` (check is blanked by a review's clearing)
          "model", "for_task",  # a follower's model; the Qwen task a takeover finishes
          "lead_input", "lead_cache_write", "lead_output")  # lead_tokens = their sum
PILOT_FIELDS = ("arm", "full_diff_read", "lead_cache_read", "detail", "commit", "tree")
REQUIRED = ("repo", "task_id", "summary", "route", "reason", "outcome")
# runtime: a `mates runtime` build; pane: a pane event (idle close); parallel: toggle `mates parallel on|off`
ROUTES = {"qwen", "claude-subagent", "claude-self", "runtime", "pane", "parallel"}
OUTCOMES = {"accepted", "sent-back", "escalated", "failed", "closed"}  # closed: a pane event


def validate(rec: dict) -> dict:
    unknown = set(rec) - set(FIELDS) - set(PILOT_FIELDS)
    if unknown:
        raise ValueError(f"unknown log fields: {sorted(unknown)}")
    missing = [k for k in REQUIRED if k not in rec]
    if missing:
        raise ValueError(f"missing log fields: {missing}")
    if rec["route"] not in ROUTES:
        raise ValueError(f"bad route {rec['route']!r}")
    if rec["outcome"] not in OUTCOMES:
        raise ValueError(f"bad outcome {rec['outcome']!r}")
    if rec.get("arm") not in (None, "mates", "solo"):
        raise ValueError(f"bad arm {rec['arm']!r}")
    return rec


def append(path, rec: dict) -> None:
    rec = validate(dict(rec))
    rec.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    line = (json.dumps(rec, sort_keys=True) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def write(path, rec: dict) -> None:
    """The one way to add a log line: makes the folder, validates, appends."""
    rec = validate(dict(rec))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    append(path, rec)


def last_ts(path, repo):
    """Epoch seconds of the newest line of `repo` in the log at `path`, or None (no log, no line)."""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            r = json.loads(line)
            if isinstance(r, dict) and r.get("repo") == repo:
                return calendar.timegm(time.strptime(r["ts"], "%Y-%m-%dT%H:%M:%SZ"))
        except (ValueError, KeyError, TypeError, OverflowError):
            continue
    return None
