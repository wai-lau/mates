"""mates housekeeping: open and remove Qwen panes, list them, and read or add to the log.

`add` and `remove` are the lead's (MATES_LEAD=1); `list` and `log` also run for Wai.
`remove` is the only command that drops an entry from the state.
"""
import argparse
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import cli
import gitsafe
import layout
import logrec
import session
from gitsafe import GitError, git_lead

NO_ROOM = "no room for another Qwen pane: widen the window or close a pane"
SUMMARY_MAX = 50  # table cells are cut here


def _table(headers, rows):
    rows = [[str(c) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    for r in [list(headers)] + rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())


def _cut(text, n=SUMMARY_MAX):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _open_panes(root, state):
    """{pane_id: n} for the Qwen panes that are open now; an open entry whose pane Wai
    closed is marked closed (session.closed_by_wai; in `state` only: the caller saves)."""
    found = {}
    for q in state["qwens"]:
        if q.get("closed"):
            continue
        hit = session.find_pane(root, q["n"])
        if hit is not None:
            found[hit[0]] = q["n"]
        elif session.closed_by_wai(root, q["n"]):
            q["closed"] = True
    return found


# --- add, remove -----------------------------------------------------------------------

def add(args):
    cli._parser("add", "mates add").parse_args(args)
    root = cli._lead_root()
    state = cli._load(root)
    lead = session.find_lead(root) or cli.die("there is no mates lead pane for this repo (start it with mates)")
    found = _open_panes(root, state)
    try:  # the Qwen panes left to right, so the new one opens right of the rightmost
        order = [w["pane"] for w in layout.pane_widths(found)]
    except RuntimeError:
        order = sorted(found.values())
    by_n = {n: pid for pid, n in found.items()}
    right = [by_n[n] for n in order if n in by_n]
    closed = sorted((q for q in state["qwens"] if q.get("closed")), key=lambda q: q["n"])
    q = closed[0] if closed else cli._entry(root, max((x["n"] for x in state["qwens"]), default=0) + 1)
    cmd = cli.qwen_cmd(q["worktree"], None, state.get("runtime"), root)
    try:  # split first: on NoRoom nothing has changed (the pane starts in root; qwen_cmd cds)
        pane = layout.split(layout.split_qwen(lead, right, root, cmd))
    except layout.NoRoom:
        cli.die(NO_ROOM, 1)
    try:
        session.ensure_worktree(root, q["n"])
        session.tag_pane(pane, root, q["n"])
    except (RuntimeError, OSError, subprocess.CalledProcessError) as e:
        subprocess.run([layout.TMUX, "kill-pane", "-t", pane], capture_output=True)
        cli.die(f"cannot open Qwen pane {q['n']}: {getattr(e, 'stderr', None) or e}", 1)
    cli.release_pane(pane)
    q.update(closed=False, opencode_session=None)
    if q not in state["qwens"]:
        state["qwens"].append(q)
    cli.save_state(root, state)
    print(f"opened Qwen pane {q['n']} ({pane}) in {q['worktree']}")
    return 0


def _remove_folder(root, n):
    """Remove Qwen worktree n's folder and its git entry. `--force`: dirtiness was
    checked first through git_safe, and without it git would run `git status` in
    Qwen's folder. A folder git does not know (a leftover) goes with rmtree: Qwen
    panes run as Wai, so Wai owns every file in it."""
    wt = gitsafe.worktree(root, n)
    git_lead(root, "worktree", "remove", "--force", str(wt), check=False)
    if wt.is_symlink():
        wt.unlink()
    elif wt.exists():
        shutil.rmtree(wt)
    git_lead(root, "worktree", "prune")


def _close_pane(root, state, q):
    """Kill entry q's pane (if open) and save the entry closed: from here Qwen cannot
    change the worktree, and if a later step refuses, `mates add` reopens it as it is."""
    hit = session.find_pane(root, q["n"])
    if hit:
        r = subprocess.run([layout.TMUX, "kill-pane", "-t", hit[0]], capture_output=True, text=True)
        if r.returncode != 0:
            cli.die(f"tmux kill-pane {hit[0]} failed: {r.stderr.strip()}", 1)
    q["closed"] = True
    cli.save_state(root, state)
    return hit


def remove(args):
    p = cli._parser("remove", "mates remove N [--force]")
    p.add_argument("n", type=int)
    p.add_argument("--force", action="store_true")
    a = p.parse_args(args)
    root = cli._lead_root()
    state = cli._load(root)
    q = cli._qentry(state, a.n)
    task = q.get("task")
    if task and not a.force:  # state only: nothing Qwen does changes this
        cli.die(f"Qwen pane {a.n} has an open task: mates merge {a.n} or mates discard {a.n} first "
                f"(or mates remove {a.n} --force)")
    # The pane goes first, so the clean check and the snapshot see the worktree's final files.
    hit = _close_pane(root, state, q)
    closed = f"its pane {hit[0]} is closed; " if hit else ""
    wt = gitsafe.worktree(root, a.n)
    dirty, readable = [], True
    if wt.exists() or wt.is_symlink():
        try:
            dirty = cli.worktree_dirty(root, a.n)
        except GitError as e:
            readable = False
            if not a.force:
                cli.die(f"cannot read Qwen worktree {a.n} ({e}); {closed}mates remove {a.n} --force "
                        "deletes it with no snapshot")
    if dirty and not a.force:
        cli.die(f"Qwen worktree {a.n} has work not merged ({', '.join(dirty[:10])}); {closed}mates add reopens "
                f"it as it is, or mates remove {a.n} --force (keeps a snapshot ref)")
    try:
        if a.force and readable and wt.exists():
            cli.keep_work(root, a.n, task, "removed")
        if task:
            cli.log_attempt(root, q, "failed")
            q["task"] = None
            cli.save_state(root, state)
        ref = f"refs/heads/{q['branch']}"
        head = git_lead(root, "rev-parse", "-q", "--verify", ref, check=False).stdout.strip()
        if head and git_lead(root, "merge-base", "--is-ancestor", head, "HEAD", check=False).returncode != 0:
            print(f"kept {q['branch']}'s commits the lead lacks on {cli._keep(root, a.n, head)}")
        _remove_folder(root, a.n)
        if head:
            git_lead(root, "branch", "-D", q["branch"])
    except GitError as e:
        cli.die(f"remove failed: {e}", 1)
    except OSError as e:
        cli.die(f"remove failed: cannot delete {wt}: {e}", 1)
    session.drop(root, a.n)
    print(f"removed Qwen pane {a.n}: its pane, worktree and branch {q['branch']}")
    return 0


# --- list --------------------------------------------------------------------------------

def list_(args):
    cli._parser("list", "mates list").parse_args(args)
    root = cli._lead_root()
    state = cli._load(root)
    rows = []
    for q in state["qwens"]:  # read-only: list never saves (I3: a list after a reboot kept nothing)
        hit = session.find_pane(root, q["n"])
        pane = "-" if hit is None else hit[0] + (" failed" if hit[1] == "failed" else "")
        if not gitsafe.worktree(root, q["n"]).exists():
            dirty = "no worktree"
        else:
            try:
                dirty = "yes" if cli.worktree_dirty(root, q["n"]) else "no"
            except GitError:
                dirty = "?"
        t = q.get("task")
        task = "-" if not t else f"{t['id']} " + _cut(t.get("summary") or "(Wai's own work)", 30)
        rows.append([q["n"], pane, q["branch"], dirty, task, t.get("attempts", 0) if t else "-",
                     q.get("opencode_session") or "-", "yes" if q.get("closed") else "no"])
    _table(["n", "pane", "branch", "dirty", "open task", "attempts", "OpenCode session", "closed"], rows)
    return 0


# --- the log -----------------------------------------------------------------------------

def _records():
    try:
        lines = cli.LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return None
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def log(args):
    p = argparse.ArgumentParser(prog="mates log", usage="mates log [--repo [PATH]] [--route R] [--outcome O]")
    p.add_argument("--repo", nargs="?", const="", help="this repo, or PATH")
    p.add_argument("--route", choices=sorted(logrec.ROUTES))
    p.add_argument("--outcome", choices=sorted(logrec.OUTCOMES))
    a = p.parse_args(args)
    repo = None
    if a.repo is not None:
        repo = str(Path(a.repo).resolve()) if a.repo else cli._lead_root()
    recs = _records()
    if recs is None:
        print(f"mates: no log yet ({cli.LOG})", file=sys.stderr)
        return 0
    rows = [[r.get("ts", ""), Path(r.get("repo", "")).name, r.get("task_id", ""), r.get("route", ""),
             r.get("outcome", ""), r.get("attempts", ""), "-" if r.get("pane") is None else r["pane"],
             _cut(r.get("summary"))]
            for r in recs
            if (repo is None or r.get("repo") == repo) and (a.route is None or r.get("route") == a.route)
            and (a.outcome is None or r.get("outcome") == a.outcome)]
    _table(["ts", "repo", "task", "route", "outcome", "attempts", "pane", "summary"], rows)
    return 0


def log_route(args):
    p = argparse.ArgumentParser(prog="mates log-route",
                                usage='mates log-route <route> "<summary>" "<reason>" [--outcome accepted]')
    p.add_argument("route", choices=sorted(logrec.ROUTES - {"qwen"}))  # qwen lines come from the pane commands
    p.add_argument("summary")
    p.add_argument("reason")
    p.add_argument("--outcome", choices=sorted(logrec.OUTCOMES), default="accepted")
    a = p.parse_args(args)
    task_id = uuid.uuid4().hex[:12]
    cli.LOG.parent.mkdir(parents=True, exist_ok=True)
    logrec.append(cli.LOG, {"repo": cli._lead_root(), "task_id": task_id, "summary": a.summary, "route": a.route,
                            "reason": a.reason, "outcome": a.outcome})
    print(f"logged task {task_id}: {a.route} {a.outcome}")
    return 0
