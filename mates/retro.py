"""`mates retro`: the daily retro of the mates audit log.

For each day it reads that day's log lines, cut to the lead's own words (question
text removed, each flag cut to its kind, `detail` dropped: those are the parts Qwen
wrote or chose), plus the current lessons and the last 7 retros, and asks a no-tools
`claude -p` for findings, a new lessons list and proposals. It writes
`~/.claude/mates/retros/<day>.md`, rewrites `mates/lead-lessons.md` (committed only
when the mates checkout is on main and that file is its only change) and appends the
proposals to the checkout's gitignored OPEN-QUESTIONS.md. Nothing else is changed.

With no --date it covers every day after the last retro file, up to yesterday (UTC,
the log's clock), so a late or missed run loses nothing.
"""
import argparse
import datetime
import fcntl
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import claudejson
import cli
import logrec

SRC = cli.HERE.parent  # the mates source checkout: the one ~/bin/mates resolves to
NEUTRAL = Path("/srv/mates/neutral")  # outside the home folder: no CLAUDE.md is found from here
PROMPT = cli.HERE / "prompts" / "retro.md"
CLAUDE_ARGS = ["-p", "--tools", "", "--strict-mcp-config", "--setting-sources", "project",
               "--output-format", "json"]
CLAUDE_TIMEOUT_S = 600
LAST_RETROS = 7
LESSON_MAX = 200
LESSONS_MAX = 20
LESSONS_REL = "mates/lead-lessons.md"
NO_LESSONS = "None yet. The daily retro rewrites this section.\n"
LOG_HEADER = "## Log lines"
# What the retro may see of a log line: the lead's (or Wai's) words and the numbers.
# `detail` is left out: it holds check output tails, which Qwen's code wrote.
SAFE_FIELDS = (set(logrec.FIELDS) | set(logrec.PILOT_FIELDS)) - {"detail"}
LEADING_PATH = re.compile(r"(?:^|[\s(\[\"'`])~?/(?=[\w.~-])")
FILE_PATH = re.compile(r"[\w.-]+/[\w./-]*\.[A-Za-z]\w{0,7}\b")


class RetroError(Exception):
    pass


def _today():
    return datetime.datetime.now(datetime.timezone.utc).date()


def _retros_dir():
    return cli.LOG.parent / "retros"


def _records():
    if not cli.LOG.exists():
        return []
    recs = []
    for line in cli.LOG.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            recs.append(r)
    return recs


def _day(rec):
    ts = rec.get("ts")
    return ts[:10] if isinstance(ts, str) else None


def clean(rec):
    """The log line as the retro sees it."""
    out = {k: v for k, v in rec.items() if k in SAFE_FIELDS}
    if "questions" in out:  # the count stays; only the answers (the lead's or Wai's) are kept
        out["questions"] = [{"a": q.get("a")} if isinstance(q, dict) else {} for q in out["questions"] or []]
    if "flags" in out:  # `kind:path` -> `kind`: the path is Qwen's choice
        out["flags"] = [str(f).split(":", 1)[0] for f in out["flags"] or []]
    return out


def _names(recs):
    """Repo and file names that appear in the log: a lesson must name none of them."""
    names = set()
    for r in recs:
        if isinstance(r.get("repo"), str) and r["repo"]:
            names.add(Path(r["repo"]).name)
        for f in r.get("flags") or []:
            path = str(f).split(":", 1)[1] if ":" in str(f) else ""
            if path:
                names.update({path, Path(path).name})
    return {n.lower() for n in names if n.strip()}


def check_lesson(lesson, names):
    """None if the lesson may go into lead-lessons.md, else why not."""
    if not lesson:
        return "empty"
    if len(lesson) > LESSON_MAX:
        return f"longer than {LESSON_MAX} characters"
    if LEADING_PATH.search(lesson) or FILE_PATH.search(lesson):
        return "looks like a path"
    low = lesson.lower()
    for n in sorted(names):
        if re.search(r"(?<![\w.-])" + re.escape(n) + r"(?![\w-])", low):
            return f"names {n!r} from the log"
    return None


def sort_lessons(lessons, names):
    kept, rejected = [], []
    for raw in lessons:
        lesson = " ".join(str(raw).split())
        why = check_lesson(lesson, names)
        if why is None and len(kept) >= LESSONS_MAX:
            why = f"over the {LESSONS_MAX}-lesson cap"
        (rejected.append((lesson, why)) if why else kept.append(lesson))
    return kept, rejected


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def _prompt(day, lines, lessons_text, prior):
    parts = [PROMPT.read_text(), "## Current lessons\n", lessons_text.strip() + "\n"]
    parts.append(f"## The last {LAST_RETROS} retros\n")
    parts += [p.read_text().strip() + "\n" for p in prior] or ["None yet.\n"]
    parts.append(f"{LOG_HEADER} for {day} (one JSON object per line)\n")
    parts.append("".join(json.dumps(clean(r), sort_keys=True) + "\n" for r in lines))
    return "\n".join(parts)


def ask_claude(prompt):
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # the subscription, not the API
    NEUTRAL.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run([cli._claude(), *CLAUDE_ARGS], input=prompt, capture_output=True, text=True,
                           timeout=CLAUDE_TIMEOUT_S, cwd=str(NEUTRAL), env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RetroError(f"claude did not run: {e}")
    if r.returncode != 0:
        raise RetroError(f"claude exited {r.returncode}: {r.stderr.strip()[-300:]}")
    text = claudejson.parse(r.stdout)["result"].strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        d = json.loads(text)
    except ValueError:
        raise RetroError("claude's answer is not JSON")
    if not (isinstance(d, dict) and isinstance(d.get("findings_md"), str)
            and isinstance(d.get("lessons"), list) and isinstance(d.get("proposals", []), list)):
        raise RetroError("claude's answer lacks findings_md, lessons or proposals")
    return d["findings_md"], d["lessons"], [" ".join(str(p).split()) for p in d.get("proposals") or [] if str(p).strip()]


def _git(*a):
    return subprocess.run(["git", "-C", str(SRC), *a], capture_output=True, text=True)


def commit_lessons(day, changed):
    """Commit lead-lessons.md if allowed; returns what happened, for the retro file."""
    if not changed:
        return "lessons unchanged: nothing to commit"
    branch = _git("symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    if branch != "main":
        return f"not committed: the mates checkout is on {branch or 'a detached HEAD'}, not main"
    st = _git("status", "--porcelain")
    if st.returncode != 0:
        return f"not committed: git status failed: {st.stderr.strip()}"
    others = [l[3:] for l in st.stdout.splitlines() if l[3:] != LESSONS_REL]
    if others:
        return "not committed: the mates checkout has other changes: " + ", ".join(others)
    r = _git("commit", "-q", "-m", f"mates: lessons from retro {day}", "--", LESSONS_REL)
    if r.returncode != 0:
        return f"not committed: git commit failed: {(r.stderr or r.stdout).strip()[-300:]}"
    return "committed " + _git("rev-parse", "--short", "HEAD").stdout.strip()


def append_proposals(day, proposals):
    """Each proposal becomes a numbered question under `## Open` in OPEN-QUESTIONS.md."""
    if not proposals:
        return
    path = SRC / "OPEN-QUESTIONS.md"
    text = path.read_text() if path.exists() else \
        "# Open questions\n\nUpdated -\n\n## Open\n\n## Offers\n\n## Status that bears on the questions\n"
    n = max([int(x) for x in re.findall(r"^\*\*(\d+)\.", text, re.M)] or [0])
    entries = ""
    for p in proposals:
        n += 1
        entries += (f"**{n}. Should mates take this retro proposal: {p.rstrip('.?')}?**\n\n"
                    f"Proposed by the daily retro for {day}; nothing was applied.\n\n")
    m = re.search(r"^## Open[ \t]*\n", text, re.M)
    if not m:
        text = text.rstrip("\n") + "\n\n## Open\n\n" + entries
    else:
        nxt = re.search(r"^## ", text[m.end():], re.M)
        at = m.end() + nxt.start() if nxt else len(text)
        head = text[:at].rstrip("\n") + "\n\n"
        text = head + entries + text[at:]
    stamp = time.strftime("Updated %Y-%m-%d %H:%M")
    text = re.sub(r"^Updated .*$", stamp, text, count=1, flags=re.M)
    _write(path, text)


def run_day(day, lines, names):
    lessons_path = SRC / LESSONS_REL
    old = lessons_path.read_text() if lessons_path.exists() else ""
    prior = sorted(p for p in _retros_dir().glob("*.md") if p.stem < day)[-LAST_RETROS:]
    findings, lessons, proposals = ask_claude(_prompt(day, lines, old, prior))
    kept, rejected = sort_lessons(lessons, names)
    new = "## Lessons\n\n" + ("".join(f"- {l}\n" for l in kept) if kept else NO_LESSONS)
    if new != old:
        _write(lessons_path, new)
    committed = commit_lessons(day, new != old)
    append_proposals(day, proposals)
    out = [f"# Retro {day}\n", findings.strip() + "\n",
           f"## Lessons\n\nKept {len(kept)} of {len(lessons)} in {LESSONS_REL}; {committed}.\n"]
    if rejected:
        out.append("Rejected:\n\n" + "".join(f"- {l} ({why})\n" for l, why in rejected))
    out.append("## Proposals (in OPEN-QUESTIONS.md, not applied)\n\n"
               + ("".join(f"- {p}\n" for p in proposals) if proposals else "None.\n"))
    _write(_retros_dir() / f"{day}.md", "\n".join(out))
    print(f"retro {day}: {len(lines)} log lines, {len(kept)} lessons kept, {len(rejected)} rejected, "
          f"{len(proposals)} proposals; {committed}")


def _days(recs, date):
    if date:
        return [date]
    have = sorted({_day(r) for r in recs if _day(r)})
    last = max((p.stem for p in _retros_dir().glob("*.md") if re.fullmatch(r"\d{4}-\d\d-\d\d", p.stem)), default="")
    yesterday = (_today() - datetime.timedelta(days=1)).isoformat()
    return [d for d in have if last < d <= yesterday]


def main(args):
    p = argparse.ArgumentParser(prog="mates retro", usage="mates retro [--date YYYY-MM-DD]")
    p.add_argument("--date", type=lambda s: datetime.date.fromisoformat(s).isoformat())
    a = p.parse_args(args)
    cli.LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(cli.LOG.parent / "retro.lock", "w") as lk:  # one retro at a time
        fcntl.flock(lk, fcntl.LOCK_EX)
        recs = _records()
        names = _names(recs)
        days = _days(recs, a.date)
        if not days:
            print("retro: nothing to do (no log lines since the last retro)")
        for day in days:
            lines = [r for r in recs if _day(r) == day]
            if not lines:
                print(f"retro {day}: no log lines, skipped")
                continue
            try:
                run_day(day, lines, names)
            except RetroError as e:
                cli.die(f"retro {day}: {e}", 1)
    return 0
