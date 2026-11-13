"""The mates audit log: one validated JSON line per task."""
import json
import os
import time

FIELDS = ("ts", "repo", "task_id", "summary", "route", "reason", "check", "check_sha", "red_on_base",
          "flags", "attempts", "outcome", "answered_by", "worker_wall_s", "worker_tokens", "verdict",
          "lead_tokens", "questions", "pane", "runtime", "restarted", "event", "n")
PILOT_FIELDS = ("arm", "full_diff_read", "lead_cache_read", "detail", "commit", "tree")
REQUIRED = ("repo", "task_id", "summary", "route", "reason", "outcome")
ROUTES = {"qwen", "claude-subagent", "claude-self", "runtime"}  # runtime: a `mates runtime` build
OUTCOMES = {"accepted", "sent-back", "escalated", "failed"}


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
