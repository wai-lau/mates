"""`mates retro`: the daily retro of the mates audit log.

For each day it reads that day's log lines, cut to the lead's own words (question
text removed, each flag cut to its kind, `detail` dropped: those are the parts Qwen
wrote or chose), plus the messages sent to Qwen that day (the leads' briefs and
answers, and Wai's own messages, from OpenCode's sessions; never Qwen's replies, so
Qwen's words can't become lessons), plus the current lessons and the last 7 retros, and asks
a no-tools `claude -p` for findings, lessons to add, lessons to retire and proposals. It
writes `~/.claude/mates/retros/<day>.md`, applies the lessons to `mates/lead-lessons.md`
itself, with no one's approval (Wai, 2026-11-16: "The agent that runs the retro should be
the one to append the lessons, do this without waiting for approval"): new lessons are
appended, at most RETIRE_MAX current ones are retired a day, the list stays under
LESSONS_MAX, and every lesson passes `check_lesson`. The file is committed only when the
mates checkout is on main and that file is its only change. Proposals go to the
checkout's gitignored OPEN-QUESTIONS.md. Nothing else is changed.

Chat lines are capped per day (CHAT_MAX, 300000 characters); when the cap is hit
the oldest lines go first, and a line says how many were dropped. Each chat
line is at most 2000 characters (long messages are cut).

With no --date it covers every day after the last retro file, up to yesterday (UTC,
the log's clock), so a late or missed run loses nothing.
"""
import argparse
import datetime
import fcntl
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

import claudejson
import cli
import fsutil
import logrec
import session

SRC = cli.HERE.parent  # the mates source checkout: the one ~/bin/mates resolves to
PROMPT = cli.HERE / "prompts" / "retro.md"
CLAUDE_ARGS = ["-p", "--tools", "", "--strict-mcp-config", "--setting-sources", "project",
               "--output-format", "json"]
CLAUDE_TIMEOUT_S = 600
# The fixed footer of every mates message, with a worktree path or without one (chats from before
# the footer named it): noise for the retro.
_PRE, _POST = cli.ASK_SUFFIX.split("{worktree}/")
ASK_FOOTER = re.compile("(?:" + re.escape(_PRE) + r"(?:\S+/)?" + re.escape(_POST) + r"\s*)+$")
LAST_RETROS = 7
LESSON_MAX = 200
LESSONS_MAX = 20
RETIRE_MAX = 3  # current lessons one retro may retire; the rest wait for the next day's retro
LESSONS_REL = "mates/lead-lessons.md"
NO_LESSONS = "None yet. The daily retro adds to this list.\n"
LOG_HEADER = "## Log lines"
CHAT_MAX = 300000  # characters of chat lines per day; past this, oldest Qwen lines go first
CHAT_PER_LINE = 2000  # each chat line is at most this many characters
# What the retro may see of a log line: the lead's (or Wai's) words and the numbers.
# `detail` is left out: it holds check output tails, which Qwen's code wrote.
SAFE_FIELDS = (set(logrec.FIELDS) | set(logrec.PILOT_FIELDS)) - {"detail"}
LEADING_PATH = re.compile(r"(?:^|[\s(\[\"'`])~?/(?=[\w.~-])")
FILE_PATH = re.compile(r"[\w.-]+/[\w./-]*\.[A-Za-z]\w{0,7}\b")
NAME_MIN = 6
# File names too plain to mark a lesson as naming a file (a flagged `check` or `Makefile`
# must not reject every lesson that uses the word).
COMMON = set("""
about above action active added adding after again always answer answers before behind better branch
broken bugfix change changed changes cheaper checks client commit commits common config configs
create created delete deploy design detail diffed docker during either enough errors except failed
failing figure finish findings fixture fixtures follow format gemfile handle handler helper
helpers import inputs install itself justfile lesson lessons letter levels library linter little
logger logging makefile master merged method module modules needed number object option options
others output outputs parser passed plugin prompt public python reason recent report reports readme
remove result results return review reviews rename routes runner sample sanity schema script scripts
second server should simple single source spaces string struct styles summary system target tested
testing tests things update useful values verify version within worker workers writer
""".split())


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
        out["questions"] = [{"a": q.get("a")} if isinstance(q, dict) else {} for q in _list(out["questions"])]
    if "flags" in out:  # `kind:path` -> `kind`: the path is Qwen's choice
        out["flags"] = [str(f).split(":", 1)[0] for f in _list(out["flags"])]
    return out


def _list(v):
    return v if isinstance(v, list) else []  # never iterate a string


def _chat_db(repo):
    """The OpenCode database for this repo; None if it is not there."""
    try:
        p = session.opencode_db(repo)
    except (OSError, subprocess.CalledProcessError):
        return None
    return p if p.exists() else None


def _qwen_line(p, from_role, ts):
    """One chat line as the retro sees it."""
    out = {"from": from_role, "time": ts}
    for part in p:
        if part.get("type") == "text":
            out["text"] = (out.get("text") or "") + part.get("text", "")
        elif part.get("type") == "tool":
            tool = part.get("tool")
            state = part.get("state") or {}
            inp = state.get("input") or {}
            if tool == "bash":
                cmd = inp.get("command")
            elif tool == "read":
                cmd = f"read {inp.get('filePath')}"
            elif tool == "edit":
                cmd = f"edit {inp.get('filePath')}"
            elif tool == "write":
                cmd = f"write {inp.get('filePath')}"
            else:
                cmd = tool
            if cmd:
                out.setdefault("tools", []).append(cmd)
    if out.get("text"):
        out["text"] = " ".join(out["text"].split())
    return out


def _chat_lines(repo, db, day):
    """The messages sent INTO Qwen's chats on `day` in this repo, each as a JSON object:
    "from" is "lead" (starting [from Claude]) or "other" (Wai typing, or a lead pasting by
    hand). Qwen's own replies and tool calls never go in: only Claude authors lessons, and
    Qwen's words must not reach them (Wai, 2026-11-13: "retro should not use Qwen at all")."""
    lines = []
    for mid, ms, raw, parts in session.opencode_chats(repo, db, day):
        ts = datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime("%H:%M:%S")
        msg = json.loads(raw)
        role = msg.get("role")
        p = [json.loads(part_data) for _, part_data in parts]
        if role == "user":
            text = "".join(pt.get("text", "") for pt in p if pt.get("type") == "text")
            if text.startswith("[from Claude]"):
                fr = "lead"
            else:
                fr = "other"
            line = _qwen_line(p, fr, ts)
            if "text" in line:
                t = line["text"]
                t = ASK_FOOTER.sub("", t)
                line["text"] = " ".join(t.split())
            lines.append(line)
    return lines


def _distinctive(name):
    """A flagged path part counts as a file name only if it could not be a plain word."""
    return "/" in name or "." in name or (len(name) >= NAME_MIN and name.lower() not in COMMON)


def _names(recs):
    """Repo names and flagged file names in the log: a lesson must name none of them."""
    names = set()
    for r in recs:
        if isinstance(r.get("repo"), str) and r["repo"].strip("/"):
            names.add(Path(r["repo"]).name)
        for f in _list(r.get("flags")):
            path = str(f).split(":", 1)[1].strip() if ":" in str(f) else ""
            names.update(n for n in {path, *Path(path).parts} if n and n != "/" and _distinctive(n))
    return {n.lower() for n in names if n.strip()}


# The retro reads Qwen's chat text, and the lessons load into every lead's system prompt, so a
# lesson that weakens review or runs something is refused whatever its wording (security
# audit, 2026-11-13): it may be a prompt-injected Qwen's words echoed back.
UNSAFE_LESSON = re.compile(
    r"\b(curl|wget|sudo|chmod|chown|ssh|scp|eval|base64|nc|netcat)\b|\|\s*(ba)?sh\b|\brm\s+-\w*r"
    r"|\bskip\w*\s+(\w+\s+){0,3}(diff|check|review|tests?|guard|flags?)"
    r"|\bwithout\s+(\w+\s+){0,2}(diff|check|review|tests?)"
    r"|\btrust\w*\b|\bapproved by\b|\bstraight away\b|\bright away\b"
    r"|\bignore\w*\s+(\w+\s+){0,2}(rules?|lead\.md|flags?|guards?|warnings?)"
    r"|\b(disable|bypass|override)\w*\b", re.I)


def check_lesson(lesson, names):
    """None if the lesson may go into lead-lessons.md, else why not."""
    if not lesson:
        return "empty"
    if len(lesson) > LESSON_MAX:
        return f"longer than {LESSON_MAX} characters"
    if UNSAFE_LESSON.search(lesson):
        return "reads as weakening review or running a command"
    if LEADING_PATH.search(lesson) or FILE_PATH.search(lesson):
        return "looks like a path"
    low = lesson.lower()
    for n in sorted(names):
        if re.search(r"(?<![\w.-])" + re.escape(n) + r"(?![\w-])", low):
            return f"names {n!r} from the log"
    return None


def sort_lessons(lessons, names, have=()):
    """Split new lessons into those that may be appended after `have` and (lesson, why) rejects."""
    kept, rejected = [], []
    for raw in lessons:
        lesson = " ".join(str(raw).split())
        why = check_lesson(lesson, names)
        if why is None and (lesson in have or lesson in kept):
            why = "already in the list"
        if why is None and len(have) + len(kept) >= LESSONS_MAX:
            why = f"over the {LESSONS_MAX}-lesson cap"
        (rejected.append((lesson, why)) if why else kept.append(lesson))
    return kept, rejected


def current_lessons(text):
    return [l[2:] for l in text.splitlines() if l.startswith("- ")]


def pick_retired(retire, current):
    """The current lessons to retire (1-based numbers, at most RETIRE_MAX, in the order asked)
    and a note on each number not taken."""
    taken, notes = [], []
    for raw in retire:
        n = raw if isinstance(raw, int) and not isinstance(raw, bool) else None
        if n is None or not 1 <= n <= len(current):
            notes.append(f"{raw!r}: not a lesson number")
        elif n in taken:
            continue
        elif len(taken) >= RETIRE_MAX:
            notes.append(f"{n}: over the {RETIRE_MAX}-a-day retire cap, kept for now")
        else:
            taken.append(n)
    return taken, notes


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def _chat_prompt(day, chats):
    """The chat section of the retro prompt. CHAT_MAX caps the day's chat lines across all
    repos together; past it, the oldest Qwen lines go first (lead and Wai lines stay)."""
    rows = []  # (time, repo, unused, json line), every repo
    for repo, lines in chats.items():
        for l in lines:
            l = dict(l)
            if len(l.get("text") or "") > CHAT_PER_LINE:  # cut the text, so each line stays JSON
                l["text"] = l["text"][:CHAT_PER_LINE] + " [cut]"
            rows.append((l.get("time") or "", repo, l.get("from") == "qwen", json.dumps(l, sort_keys=True)))
    total = sum(len(r[3]) + 1 for r in rows)
    drop = set()
    for i in sorted(range(len(rows)), key=lambda i: rows[i][0]):
        if total <= CHAT_MAX:
            break
        drop.add(i)
        total -= len(rows[i][3]) + 1
    parts = []
    if drop:
        parts.append(f"[{len(drop)} earlier chat line(s) dropped to stay under {CHAT_MAX} characters]")
    for repo in chats:
        kept = [r[3] for i, r in enumerate(rows) if r[1] == repo and i not in drop]
        parts.append(f"## Messages sent to Qwen on {day} in {Path(repo).name} (one JSON line per message)")
        parts.append("Each line is a message sent to Qwen: \"from\": \"lead\" (a lead's brief or answer) or \"other\" (Wai, or a lead pasting by hand).")
        parts.append("\n".join(kept))
    return "\n\n".join(parts)


def _prompt(day, lines, lessons_text, prior, chats):
    current = current_lessons(lessons_text)
    numbered = "".join(f"{i}. {l}\n" for i, l in enumerate(current, 1)) or "None yet.\n"
    parts = [PROMPT.read_text(), "## Current lessons (numbered for `retire`)\n", numbered]
    parts.append(f"## The last {LAST_RETROS} retros\n")
    parts += [p.read_text().strip() + "\n" for p in prior] or ["None yet.\n"]
    parts.append(f"{LOG_HEADER} for {day} (one JSON object per line)\n")
    parts.append("".join(json.dumps(clean(r), sort_keys=True) + "\n" for r in lines))
    if chats:
        parts.append(_chat_prompt(day, chats))
    return "\n".join(parts)


def ask_claude(prompt):
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # the subscription, not the API
    try:
        with fsutil.private_cwd() as cwd:  # fresh and private: nothing planted there can run as Wai
            r = subprocess.run([cli._claude(), *CLAUDE_ARGS], input=prompt, capture_output=True, text=True,
                               timeout=CLAUDE_TIMEOUT_S, cwd=cwd, env=env)
    except (OSError, subprocess.TimeoutExpired, fsutil.UnsafeDir) as e:
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
            and all(isinstance(d.get(k, []), list) for k in ("add", "retire", "proposals"))):
        raise RetroError("claude's answer lacks findings_md, or add, retire or proposals is not a list")
    proposals = [" ".join(str(p).split()) for p in d.get("proposals") or [] if str(p).strip()]
    return d["findings_md"], d.get("add") or [], d.get("retire") or [], proposals


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


def run_day(day, lines, names, chats):
    lessons_path = SRC / LESSONS_REL
    old = lessons_path.read_text() if lessons_path.exists() else ""
    prior = sorted(p for p in _retros_dir().glob("*.md") if p.stem < day)[-LAST_RETROS:]
    findings, add, retire, proposals = ask_claude(_prompt(day, lines, old, prior, chats))
    current = current_lessons(old)
    taken, notes = pick_retired(retire, current)
    have = [l for i, l in enumerate(current, 1) if i not in taken]
    added, rejected = sort_lessons(add, names, have)
    kept = have + added
    new = old if kept == current else \
        "## Lessons\n\n" + ("".join(f"- {l}\n" for l in kept) if kept else NO_LESSONS)
    if new != old:
        _write(lessons_path, new)
    committed = commit_lessons(day, new != old)
    append_proposals(day, proposals)
    out = [f"# Retro {day}\n", findings.strip() + "\n",
           f"## Lessons\n\nAdded {len(added)}, retired {len(taken)}; {len(kept)} in {LESSONS_REL}; {committed}.\n"]
    if added:
        out.append("Added:\n\n" + "".join(f"- {l}\n" for l in added))
    if taken:
        out.append("Retired:\n\n" + "".join(f"- {current[n - 1]}\n" for n in taken))
    if notes:
        out.append("Not retired:\n\n" + "".join(f"- {x}\n" for x in notes))
    if rejected:
        out.append("Rejected:\n\n" + "".join(f"- {l} ({why})\n" for l, why in rejected))
    out.append("## Proposals (in OPEN-QUESTIONS.md, not applied)\n\n"
               + ("".join(f"- {p}\n" for p in proposals) if proposals else "None.\n"))
    _write(_retros_dir() / f"{day}.md", "\n".join(out))
    print(f"retro {day}: {len(lines)} log lines, {len(added)} lessons added, {len(taken)} retired, "
          f"{len(rejected)} rejected, {len(proposals)} proposals; {committed}")


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
        # Chat data is read once: all repos, all days the retro covers
        repos = {r.get("repo") for r in recs if r.get("repo")}
        chat_dbs = {}
        for repo in repos:
            db = _chat_db(repo)
            if db:
                chat_dbs[repo] = db
        chats_for = {}
        for day in days:
            for repo, db in chat_dbs.items():
                chats_for[(day, repo)] = _chat_lines(repo, db, day)
        for day in days:
            lines = [r for r in recs if _day(r) == day]
            chats = {}
            for repo, db in chat_dbs.items():
                cl = chats_for.get((day, repo))
                if cl:
                    chats[repo] = cl
            if not lines and not chats:
                print(f"retro {day}: no log lines, skipped")
                continue
            try:
                run_day(day, lines, names, chats)
            except RetroError as e:
                cli.die(f"retro {day}: {e}", 1)
    return 0
