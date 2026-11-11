"""mates: Claude Code (the lead) on the left, sandboxed Qwen panes on the right.

`bin/mates` picks the tmux window and runs `cli.py launch` in the lead's pane:
that starts the Qwen panes, saves the state and prints the lead's `claude` argv
NUL-separated, which `bin/mates` runs as its child. The other subcommands are
the lead's tools and refuse unless MATES_LEAD=1 (except list, log and retro).
"""
import argparse
import contextlib
import copy
import hashlib
import http.client
import json
import os
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

if __name__ == "__main__":  # panes.py imports cli: let it get this module, not a second copy
    sys.modules.setdefault("cli", sys.modules[__name__])

import fsutil
import gitsafe
import guard
import layout
import logrec
import panes
import runtime
import session
from gitsafe import GitError, git_lead, git_safe

ASK_SUFFIX = ("(If any name, file or expected result above is not stated exactly, write your question "
              "to .mates/question.md and stop before editing. Do not edit tests unless told.)")
HERE = Path(__file__).resolve().parent
LEAD_PROMPT = (HERE / "lead.md", HERE / "lead-lessons.md")
QWEN_LIVE = "@mates-qwen-live"  # set on a Qwen pane while its qwen runs (qwen_cmd)
OPEN_COMMANDS = {"launch", "list", "log", "retro"}  # the rest run only from the lead (MATES_LEAD=1)


def die(msg, code=2):
    print(f"mates: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _claude():
    return os.environ.get("MATES_CLAUDE") or "claude"


def _entry(root, n):
    return {"n": n, "worktree": str(Path(root) / ".claude" / "worktrees" / f"mates-qwen-{n}"),
            "branch": f"mates/qwen-{n}", "opencode_session": None, "closed": False, "task": None}


# --- the Qwen pane wrapper ----------------------------------------------------------

def qwen_cmd(worktree, opencode_session=None, runtime_dir=None, root=None):
    """The command a Qwen pane runs. It waits for `release_pane` (so the pane is
    tagged before qwen can fail), then runs qwen in the worktree with read-only git.
    While qwen runs the pane has `@mates-qwen-live=1` (`send` and `answer` need it:
    `#{pane_current_command}` would only ever show this wrapper's bash).
    Exit 0 closes the pane; any other exit marks it `@mates-failed=1` (the tag
    stays) and leaves a shell with the error. That shell runs as Wai, outside the
    sandbox, so it starts in the lead repo `root` (default: the worktree's repo),
    never among Qwen's files."""
    root = root or Path(worktree).parents[2]
    env = [f"QWEN_ROOT={shlex.quote(str(worktree))}", "QWEN_GIT_RO=1"]
    if runtime_dir:
        env.append(f"QWEN_RUNTIME={shlex.quote(str(runtime_dir))}")
    run = [*env, shlex.quote(session._qwen())]
    if opencode_session:
        run += ["-s", shlex.quote(opencode_session)]
    script = ('timeout 30 tmux wait-for "mates-go-$TMUX_PANE"; '
              f'cd {shlex.quote(str(worktree))} && tmux set-option -p -t "$TMUX_PANE" {QWEN_LIVE} 1 && '
              f'{" ".join(run)}; rc=$?; '
              f'tmux set-option -p -u -t "$TMUX_PANE" {QWEN_LIVE}; '
              '[ "$rc" = 0 ] && exit 0; '
              'tmux set-option -p -t "$TMUX_PANE" @mates-failed 1; '
              'echo "mates: qwen exited $rc; this pane is marked failed (mates send refuses it), '
              'a shell instead" >&2; '
              f'cd {shlex.quote(str(root))} || cd /; '
              'echo "mates: this shell is OUTSIDE the sandbox, in the lead repo $PWD (not the Qwen worktree)" >&2; '
              'exec "${SHELL:-bash}"')
    return "exec bash -c " + shlex.quote(script)


def release_pane(pane):
    """Let a pane started with `qwen_cmd` run qwen (call after tagging it)."""
    subprocess.run([layout.TMUX, "wait-for", "-S", f"mates-go-{pane}"], capture_output=True)


# --- the plan (no tmux, no git) -------------------------------------------------------

def _lead_prompt():
    files = list(LEAD_PROMPT)
    private = Path.home() / ".claude" / "private.md"
    if os.access(private, os.R_OK):
        files.append(private)
    return b"".join(f.read_bytes() for f in files).decode("utf-8", "replace").rstrip("\n")  # as "$(cat ...)"


def _claude_plan(st, root, restore, warnings):
    sid = None
    if restore:
        sid = session.claude_session(root, st["claude_title"]) or st.get("claude_session")
        if sid and not (session.PROJECTS_DIR / session.repo_key(root) / f"{sid}.jsonl").exists():
            warnings.append(f"mates: the Claude conversation {sid} is gone; starting a new one")
            sid = None
    if sid:
        args = ["--resume", sid]
    else:
        sid = str(uuid.uuid4())
        args = ["--session-id", sid, "-n", st["claude_title"]]
    st["claude_session"] = sid
    return args


def _qwen_entries(st, root, restore, warnings):
    if not restore:  # fresh: pane 1 with a new OpenCode session; the rest stay in the state, closed
        q1 = next((q for q in st["qwens"] if q["n"] == 1), None)
        if q1 is None:
            q1 = _entry(root, 1)
            st["qwens"].append(q1)
        q1.update(closed=False, opencode_session=None, task=None)
        for q in st["qwens"]:
            if q["n"] != 1:
                q["closed"] = True
        return [q1]
    entries = [q for q in st["qwens"] if not q.get("closed")]
    if not entries:
        warnings.append("mates: no Qwen pane was open; opening pane 1")
        return _qwen_entries(st, root, False, warnings)
    order = [w["pane"] for w in st.get("widths", []) if w["pane"] != "lead"]
    entries.sort(key=lambda q: (order.index(q["n"]) if q["n"] in order else len(order), q["n"]))
    try:
        db = session.opencode_db(root)
    except (OSError, subprocess.CalledProcessError) as e:
        warnings.append(f"mates: cannot find the OpenCode sessions ({e}); Qwen panes start new ones")
        db = None
    for q in entries:
        found = session.opencode_session(q["worktree"], db) if db else None
        if found is None and q.get("opencode_session"):
            warnings.append(f"mates: Qwen pane {q['n']}'s OpenCode session {q['opencode_session']} is gone; "
                            "starting a new one")
        q["opencode_session"] = found
    return entries


def launch_plan(state, root, fresh, claude_args, runtime_dir=None):
    """What `launch` starts, from the saved state (None on a first run). Pure: no
    tmux, no git. Returns {"claude_argv", "qwens": [{"n", "worktree", "cmd"}],
    "warnings", "state"}; the input state is not changed."""
    root = str(root)
    warnings = []
    restore = state is not None and not fresh
    st = copy.deepcopy(state) if state is not None else session.new_state(root)
    st["runtime"] = str(runtime_dir) if runtime_dir else None
    session_args = _claude_plan(st, root, restore, warnings)
    qwens = [{"n": q["n"], "worktree": q["worktree"],
              "cmd": qwen_cmd(q["worktree"], q["opencode_session"], runtime_dir, root)}
             for q in _qwen_entries(st, root, restore, warnings)]
    return {"claude_argv": _claude_argv(root, session_args, claude_args), "qwens": qwens,
            "warnings": warnings, "state": st}


def _claude_argv(root, session_args, claude_args):
    return ["env", "-u", "ANTHROPIC_API_KEY", "MATES_LEAD=1", f"MATES_ROOT={root}", _claude(),
            *session_args, "--append-system-prompt", _lead_prompt(), *claude_args]


# --- shared helpers ---------------------------------------------------------------------

def save_state(root, state):
    """Refresh state["widths"] while the mates window is live, then save."""
    lead = session.find_lead(root)
    if lead is not None:
        panes = {lead: "lead"}
        for q in state.get("qwens", []):
            hit = None if q.get("closed") else session.find_pane(root, q["n"])
            if hit:
                panes[hit[0]] = q["n"]
        try:
            state["widths"] = layout.pane_widths(panes)
        except RuntimeError:
            pass  # widths are cosmetic: keep the saved ones
    session.save(root, state)


def snapshot(root, n, base):
    """The tree of Qwen worktree n as it is now: a temp index seeded with `base`, then
    every change outside `.mates/` added (untracked files too). Tracked `.mates/*`
    keep their `base` version; Qwen's edits there and untracked `.mates/*` never
    reach the tree."""
    with tempfile.TemporaryDirectory(prefix="mates-snap-") as tmp:
        idx = Path(tmp) / "index"
        git_safe(root, n, "read-tree", base, index=idx, attrs=base)
        # Not `add -A -- . ':!.mates'`: git exits 1 when an excluded path is also ignored
        # (info/exclude lists .mates/). Add everything, then put .mates back as it was at base.
        git_safe(root, n, "add", "-A", "--", ".", index=idx, attrs=base)
        git_safe(root, n, "rm", "-r", "-f", "-q", "--cached", "--ignore-unmatch", "--", ".mates", index=idx, attrs=base)
        kept = git_safe(root, n, "ls-tree", "-r", "-z", "--full-tree", base, "--", ".mates", index=idx,
                        attrs=base).stdout
        if kept:
            git_safe(root, n, "update-index", "-z", "--index-info", index=idx, input=kept, attrs=base)
        return git_safe(root, n, "write-tree", index=idx, attrs=base).stdout.strip()


def worktree_dirty(root, n):
    """Paths where Qwen worktree n differs from its HEAD (untracked included, `.mates/`
    left out); empty means clean."""
    head = git_safe(root, n, "rev-parse", "HEAD^{tree}").stdout.strip()
    tree = snapshot(root, n, "HEAD")  # the worktree's HEAD: a commit mates put there
    if tree == head:
        return []
    out = git_safe(root, n, "diff", "--name-only", "-z", "--no-renames", head, tree, attrs="HEAD").stdout
    return [p for p in out.split("\0") if p]


# --- launch ------------------------------------------------------------------------------

def _root():
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if r.returncode != 0:
        die("not in a git repo")
    return r.stdout.strip()


def _warm(root):
    """`qwen warm` in the background, so the model loads while the panes start."""
    try:
        subprocess.Popen([session._qwen(), "warm"], cwd=root, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        print(f"mates: qwen warm failed: {e}", file=sys.stderr)


def _runtime(root, warnings):
    try:
        return runtime.ensure_runtime(root, "HEAD")
    except (RuntimeError, OSError, fsutil.UnsafeDir) as e:
        warnings.append(f"mates: .mates/runtime.sh failed, panes start without it: {e}")
        return None


def _parse_launch(args):
    fresh = lead_only = False
    for i, a in enumerate(args):
        if a == "--":
            return fresh, lead_only, args[i + 1:]
        if a == "--fresh":
            fresh = True
        elif a == "--lead-only":
            lead_only = True
        else:
            die(f"launch: unknown argument {a!r} (usage: launch [--fresh] [--lead-only] [-- <claude args>])")
    return fresh, lead_only, []


def _split_qwens(root, lead, plan, warnings):
    """Step 4: worktree, split, tag, release, for each planned pane left to right."""
    ids = []
    for q in plan["qwens"]:
        hit = session.find_pane(root, q["n"])
        if hit is not None:
            warnings.append(f"mates: Qwen pane {q['n']} is already open ({hit[0]}); left as it is")
            continue
        session.ensure_worktree(root, q["n"])
        try:
            pane = layout.split(layout.split_qwen(lead, [p for _, p in ids], q["worktree"], q["cmd"]))
        except layout.NoRoom:
            warnings.append(f"mates: no room for Qwen pane {q['n']} and after; widen the window and run mates again")
            break
        session.tag_pane(pane, root, q["n"])
        release_pane(pane)
        ids.append((q["n"], pane))
    return ids


def launch(args):
    fresh, lead_only, claude_args = _parse_launch(args)
    lead = os.environ.get("TMUX_PANE") or die("launch runs in the lead's tmux pane (start it with bin/mates)")
    root = _root()
    old = session.load(root)
    warnings = []
    if lead_only:  # the window is live: restart only Claude, leave the Qwen panes and their state alone
        state = copy.deepcopy(old) if old else session.new_state(root)
        argv = _claude_argv(root, _claude_plan(state, root, old is not None and not fresh, warnings), claude_args)
    else:
        _warm(root)
        plan = launch_plan(old, root, fresh, claude_args, _runtime(root, warnings))
        state, argv = plan["state"], plan["claude_argv"]
        warnings += plan["warnings"]
        if fresh and old:
            for q in old["qwens"]:
                if Path(q["worktree"]).exists():
                    try:
                        dirty = worktree_dirty(root, q["n"])
                    except (RuntimeError, subprocess.CalledProcessError) as e:
                        dirty = [f"(could not check: {getattr(e, 'stderr', None) or e})"]
                    if dirty:
                        warnings.append(f"mates: {q['worktree']} has work not merged, kept as it is: "
                                        + ", ".join(dirty))
        ids = _split_qwens(root, lead, plan, warnings)
        panes = [lead] + [p for _, p in ids]
        saved = state.get("widths") or []
        if [w["pane"] for w in saved] == ["lead"] + [n for n, _ in ids]:
            layout.restore_widths(panes, saved)
    for w in warnings:
        print(w, file=sys.stderr)
    session.tag_pane(lead, root, "lead")
    r = subprocess.run([layout.TMUX, "set-option", "-w", "-t", lead, "@mates-window", session.repo_key(root)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        die(f"tmux set-option @mates-window failed: {r.stderr.strip()}", 1)
    save_state(root, state)
    sys.stdout.buffer.write(b"".join(a.encode() + b"\0" for a in argv))
    sys.stdout.flush()
    return 0


# --- delegation: helpers ---------------------------------------------------------------

LOG = Path.home() / ".claude" / "mates" / "log.jsonl"
TEXT_MAX = 2000  # Qwen's done/question text is cut here
WAIT_DEFAULT_S = 1800
WAIT_POLL_S = 2.0
SESSION_WAIT_S = 30  # send: how long to look for the OpenCode session row after the message
SESSION_POLL_S = 0.5
CHECK_TAIL = 20
# Task 0's input-box rule, read from OpenCode 1.19.5's TUI: an empty prompt shows the
# placeholder `Ask anything… "<example>"` (`Run a command… "…"` in shell mode, which is
# not empty input); typed text replaces it. Looked for on every visible line: a fresh
# session draws the box mid-pane, under the logo (seen in Wai's first real run). The
# opening quote of the example is part of the rule, so chat text that merely says
# "Ask anything..." does not count as an empty box.
INPUT_EMPTY = re.compile(r'Ask anything(…|\.\.\.) "')
PROTOCOL = ("done.md", "question.md")


class Unsafe(Exception):
    """Qwen's .mates folder or a protocol file is not what it should be (a link, a fifo)."""


def _lead_root():
    root = os.environ.get("MATES_ROOT") or _root()
    return str(Path(root).resolve())


def _int(s, what="N"):
    try:
        return int(s)
    except ValueError:
        die(f"{what} must be a pane number, not {s!r}")


def _load(root):
    state = session.load(root)
    if state is None:
        die("no mates state for this repo (start it with mates)")
    return state


def _qentry(state, n):
    for q in state["qwens"]:
        if q["n"] == n:
            return q
    die(f"there is no Qwen pane {n}")


def _tmux_out(*args):
    r = subprocess.run([layout.TMUX, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"tmux {args[0]} failed: {r.stderr.strip()}")
    return r.stdout


def _find(root, state, q):
    """(pane_id, status) for entry q; a pane Wai closed is marked closed and saved
    (session.mark_closed: never while the lead pane is gone too)."""
    hit = session.find_pane(root, q["n"])
    if hit is None and session.mark_closed(state, q["n"]):
        save_state(root, state)
    return hit


def _live_pane(root, state, q):
    """The pane id of entry q, refusing unless OpenCode runs in it now."""
    if q.get("closed"):
        die(f"Qwen pane {q['n']} is closed (mates add opens one)")
    hit = _find(root, state, q)
    if hit is None:
        die(f"Qwen pane {q['n']} is gone" + ("; marked it closed (mates add opens one)" if q.get("closed") else ""))
    pane, status = hit
    if status == "failed":
        die(f"Qwen pane {q['n']} ({pane}) failed: qwen exited with an error; read the pane, then restart it")
    if _tmux_out("display", "-p", "-t", pane, "#{" + QWEN_LIVE + "}").strip() != "1":
        die(f"Qwen pane {q['n']} ({pane}) is not running qwen (no {QWEN_LIVE})")
    return pane


def _db(root):
    try:
        return session.opencode_db(root)
    except (OSError, subprocess.CalledProcessError) as e:
        die(f"cannot find the OpenCode sessions: {getattr(e, 'stderr', None) or e}")


def _busy(q, db):
    """Is OpenCode replying in pane q's newest session right now?"""
    sid = session.opencode_session(q["worktree"], db)
    return sid is not None and session.opencode_state(sid, db, 0) == "busy"


def input_empty(pane):
    """True when the OpenCode input box in `pane` holds no typed text."""
    return any(INPUT_EMPTY.search(line) for line in _tmux_out("capture-pane", "-p", "-t", pane).splitlines())


def paste(pane, text):
    """Deliver `text` to `pane` as one message: load a buffer, paste it bracketed
    (and delete it), then Enter (Task 0's measured recipe)."""
    buf = f"mates-{pane.lstrip('%')}"
    for args, inp in ((("load-buffer", "-b", buf, "-"), text),
                      (("paste-buffer", "-p", "-d", "-b", buf, "-t", pane), None),
                      (("send-keys", "-t", pane, "Enter"), None)):
        r = subprocess.run([layout.TMUX, *args], input=inp, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"tmux {args[0]} failed: {r.stderr.strip()}")


def _message(text):
    return f"[from Claude] {text}\n{ASK_SUFFIX}"


# Qwen's .mates folder is Qwen's: every read, delete or write there goes through a
# folder fd opened without following links, so a planted link cannot point Wai's
# process at other files (done.md -> ~/.ssh/id_ed25519 would print it to the lead).

def _mates_fd(wt, create=False):
    p = Path(wt) / ".mates"
    if create:
        with contextlib.suppress(FileExistsError):
            os.mkdir(p, 0o755)
    try:
        return os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise Unsafe(f"{p} is not a plain folder ({e.strerror})") from e


def read_protocol(wt, name):
    """The text of `.mates/<name>` (cut at TEXT_MAX), or None if there is none."""
    d = _mates_fd(wt)
    if d is None:
        return None
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=d)
        except FileNotFoundError:
            return None
        except OSError as e:
            raise Unsafe(f".mates/{name} cannot be read safely ({e.strerror})") from e
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise Unsafe(f".mates/{name} is not a plain file")
            data = os.read(fd, TEXT_MAX * 4)
        finally:
            os.close(fd)
    finally:
        os.close(d)
    return data.decode("utf-8", "replace").strip()[:TEXT_MAX]


def clear_protocol(wt):
    d = _mates_fd(wt)
    if d is None:
        return
    try:
        for name in PROTOCOL:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name, dir_fd=d)  # a link itself is removed, never its target
    finally:
        os.close(d)


def _unsafe(fn, *args):
    try:
        return fn(*args)
    except Unsafe as e:
        die(f"refusing: {e}")


def _lead_head(root):
    return git_lead(root, "rev-parse", "HEAD").stdout.strip()


def _keep(root, n, commit):
    """Point a new `refs/mates/discarded/<n>-<ts>` at `commit`; returns the ref."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for i in range(100):
        ref = f"refs/mates/discarded/{n}-{stamp}" + (f"-{i}" if i else "")
        if git_lead(root, "update-ref", ref, commit, "", check=False).returncode == 0:  # create only
            return ref
    raise GitError(f"cannot create a refs/mates/discarded/{n}-{stamp} ref")


def _snapshot_commit(root, n, base, tree, why):
    return git_lead(root, "commit-tree", tree, "-p", base, "-m", f"mates qwen-{n}: {why}").stdout.strip()


def _reset_worktree(root, n, branch, commit):
    """Put Qwen worktree n on `branch` at `commit`: tracked files as in `commit`, the
    worktree's own index matching, and untracked files outside `.mates/` removed
    (ignored ones stay). Callers keep a snapshot first if anything there matters."""
    _, admin, _ = gitsafe.dirs(root, n)
    idx = admin / "index"
    git_safe(root, n, "read-tree", "HEAD", index=idx, attrs=commit)  # so reset deletes only files HEAD tracks
    git_safe(root, n, "symbolic-ref", "HEAD", f"refs/heads/{branch}", index=idx, attrs=commit)
    git_safe(root, n, "reset", "-q", "--hard", commit, index=idx, attrs=commit)
    git_safe(root, n, "clean", "-fdq", "--", ".", ":!.mates", index=idx, attrs=commit)


def _task(base, direct=False, summary=None, reason=None, files=()):
    now = time.time()
    return {"id": uuid.uuid4().hex[:12], "summary": summary, "reason": reason, "files": list(files),
            "base": base, "direct": direct, "sent_ts": None if direct else now, "attempt_ts": now,
            "paste_ts": None if direct else now, "attempts": 0 if direct else 1, "opencode_session": None,
            "questions": [], "diffed_tree": None, "flags": [], "checked_tree": None, "check_cmd": None}


def _clear_review(task):
    task.update(checked_tree=None, check_cmd=None, diffed_tree=None, flags=[])


def answered_by():
    """[<model>] that the Ollama server `qwen host` names has loaded now ([] if none),
    or None when there is no server."""
    try:
        r = subprocess.run([session._qwen(), "host"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    host = r.stdout.strip()
    if r.returncode != 0 or not host:
        return None
    try:
        with urllib.request.urlopen(f"http://{host}/api/ps", timeout=5) as resp:
            models = json.load(resp).get("models") or []
        name = models[0].get("name") or models[0].get("model") if models else None
    except (OSError, ValueError, AttributeError, http.client.HTTPException):
        return None
    return [name] if name else []


def _costs(root, entry, t):
    """The cost fields of the attempt on entry's task `t` that just finished."""
    now = time.time()
    sid = t.get("opencode_session") or entry.get("opencode_session")
    tokens = last = None
    if sid:
        try:
            tokens, last = session.opencode_usage(sid, session.opencode_db(root), t["attempt_ts"])
        except (OSError, subprocess.CalledProcessError, sqlite3.Error) as e:
            print(f"mates: cannot read Qwen's tokens: {e}", file=sys.stderr)
    # finish: Qwen's last reply in this attempt; with none (Wai's own work), now
    rec = {"worker_wall_s": round(max(0.0, (last or now) - t["attempt_ts"]), 1), "worker_tokens": tokens,
           "answered_by": answered_by(), "lead_tokens": None, "lead_cache_read": None}
    state = session.load(root) or {}
    transcript = session.transcript_path(root, state)
    if transcript:
        rec["lead_tokens"], rec["lead_cache_read"] = session.lead_tokens(transcript, entry["n"],
                                                                         entry.get("lead_since") or 0)
    entry["lead_since"] = now  # the next attempt's lead tokens start here (the caller saves)
    return rec


def log_attempt(root, entry, outcome):
    """Append the log line for the attempt on entry's task that just finished."""
    t = entry["task"]
    checked = t.get("checked_tree")
    rec = {"repo": str(root), "task_id": t["id"], "summary": t.get("summary") or "", "route": "qwen",
           "reason": t.get("reason"), "attempts": t.get("attempts", 0), "outcome": outcome, "pane": entry["n"],
           "check": t.get("check_cmd"),
           "check_sha": hashlib.sha256(t["check_cmd"].encode()).hexdigest() if t.get("check_cmd") else None,
           "tree": checked, "flags": t.get("flags") or [],
           "full_diff_read": checked is not None and t.get("diffed_tree") == checked,
           "questions": t.get("questions") or [], **_costs(root, entry, t)}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logrec.append(LOG, rec)


def _remind(n, task):
    if task["attempts"] >= 2:
        print(f"mates: this is attempt {task['attempts']} on this task; if it fails, "
              f"mates discard {n} and do it yourself", file=sys.stderr)


def _parser(name, usage):
    return argparse.ArgumentParser(prog=f"mates {name}", usage=usage)


# --- send, wait, answer ------------------------------------------------------------------

def send(args):
    p = _parser("send", 'mates send N --reason "<why Qwen>" [--files a,b] [--continue] "<text>"')
    p.add_argument("n", type=int)
    p.add_argument("--reason", required=True)
    p.add_argument("--files", default="")
    p.add_argument("--continue", dest="cont", action="store_true")
    p.add_argument("text", nargs="+")
    a = p.parse_args(args)
    text = " ".join(a.text)
    root = _lead_root()
    state = _load(root)
    q = _qentry(state, a.n)
    pane = _live_pane(root, state, q)
    db = _db(root)
    if _busy(q, db):
        die(f"Qwen pane {a.n} is busy (OpenCode is replying); wait for it")
    if not input_empty(pane):
        die(f"Qwen pane {a.n} has text in its input box (Wai may be typing); not pasting over it")
    if not state.get("parallel"):
        others = [o["n"] for o in state["qwens"] if o["n"] != a.n and o.get("task") and not o.get("closed")]
        if others:
            die(f"pane {others[0]} has an open task and parallel is off: merge or discard it first")
    try:
        dirty = worktree_dirty(root, a.n)
    except GitError as e:
        die(f"cannot read Qwen worktree {a.n}: {e}")
    if dirty:
        die(f"Qwen worktree {a.n} has work not merged ({', '.join(dirty[:10])}): "
            f"mates merge {a.n} or mates discard {a.n} first")
    lead = _lead_head(root)
    # Qwen cannot commit, so mates/qwen-N holds no work of its own: moving it to the lead
    # HEAD is the rebase. Anything on it the lead lacks is kept on a ref first.
    head = git_safe(root, a.n, "rev-parse", "HEAD").stdout.strip()
    if git_lead(root, "merge-base", "--is-ancestor", head, lead, check=False).returncode != 0:
        print(f"mates: {q['branch']} had commits the lead lacks; kept on {_keep(root, a.n, head)}", file=sys.stderr)
    _reset_worktree(root, a.n, q["branch"], lead)
    _unsafe(clear_protocol, q["worktree"])
    files = [f for f in a.files.split(",") if f]
    summary = text.splitlines()[0][:120]
    old = q.get("task")
    if old and not old.get("direct"):  # the same task sent again: its last attempt is over
        log_attempt(root, q, "sent-back")
        now = time.time()
        old.update(summary=summary, reason=a.reason, files=files, base=lead, attempts=old["attempts"] + 1,
                   attempt_ts=now, paste_ts=now)
        _clear_review(old)
        task = old
    else:
        task = _task(lead, summary=summary, reason=a.reason, files=files)
    q["task"] = task
    before = session.opencode_session(q["worktree"], db)
    if before:
        q["opencode_session"] = before
    if a.cont:
        task["opencode_session"] = before
    save_state(root, state)
    if not a.cont:
        paste(pane, "/new")  # a clean context per task (Task 0: works; the row comes with the message)
    task["paste_ts"] = time.time()
    paste(pane, _message(text))
    if task["opencode_session"] is None:
        deadline = time.time() + SESSION_WAIT_S
        while True:
            sid = session.opencode_session(q["worktree"], db)
            if sid and sid != before:
                task["opencode_session"] = q["opencode_session"] = sid
                break
            if time.time() >= deadline:
                print(f"mates: no new OpenCode session for pane {a.n} yet; mates wait keeps looking", file=sys.stderr)
                break
            time.sleep(SESSION_POLL_S)
    save_state(root, state)
    print(f"sent to Qwen pane {a.n} (task {task['id']}, attempt {task['attempts']})")
    _remind(a.n, task)
    return 0


def _patch_task(root, n, task_id, **fields):
    """Set fields on pane n's task in the saved state, if it is still task_id."""
    state = session.load(root)
    q = next((x for x in state["qwens"] if x["n"] == n), None) if state else None
    if q and q.get("task") and q["task"]["id"] == task_id:
        q["task"].update(fields)
        if "opencode_session" in fields:
            q["opencode_session"] = fields["opencode_session"]
        save_state(root, state)


def wait(args):
    p = _parser("wait", "mates wait N [--timeout S]")
    p.add_argument("n", type=int)
    p.add_argument("--timeout", type=float, default=WAIT_DEFAULT_S)
    a = p.parse_args(args)
    root = _lead_root()
    state = _load(root)
    q = _qentry(state, a.n)
    task = q.get("task")
    since = (task.get("paste_ts") or task["attempt_ts"]) if task else time.time()
    db = _db(root)
    deadline = time.time() + a.timeout
    while True:
        hit = session.find_pane(root, a.n)
        if hit is None:  # mark it closed in the state as it is now (wait may have run for a while)
            now_state = session.load(root)
            if now_state and session.mark_closed(now_state, a.n):
                save_state(root, now_state)
            print("dead")
            return 0
        if hit[1] == "failed":
            print("failed")
            return 0
        sid = task.get("opencode_session") if task else session.opencode_session(q["worktree"], db)
        if task and sid is None:  # the row appears only with the first message (Task 0)
            new = session.opencode_session(q["worktree"], db)
            if new and new != q.get("opencode_session"):
                sid = task["opencode_session"] = q["opencode_session"] = new
                _patch_task(root, a.n, task["id"], opencode_session=new)
        # A protocol file counts only once OpenCode has stopped (check's busy rule): Qwen
        # writes done.md and then keeps replying for a while, and check refuses meanwhile.
        found = [(word, text) for name, word in (("question.md", "question"), ("done.md", "done"))
                 if (text := _unsafe(read_protocol, q["worktree"], name)) is not None]
        if found and not _busy(q, db):
            print(f"{found[0][0]}: {found[0][1]}")
            return 0
        if not found and sid and session.opencode_state(sid, db, since) == "idle":
            print("idle")
            return 0
        if time.time() >= deadline:
            print("timeout")
            return 1
        time.sleep(min(WAIT_POLL_S, max(0.0, deadline - time.time())))


def answer(args):
    p = _parser("answer", 'mates answer N [--sendback] "<text>"')
    p.add_argument("n", type=int)
    p.add_argument("--sendback", action="store_true")
    p.add_argument("text", nargs="+")
    a = p.parse_args(args)
    text = " ".join(a.text)
    root = _lead_root()
    state = _load(root)
    q = _qentry(state, a.n)
    task = q.get("task")
    if not task:
        die(f"Qwen pane {a.n} has no open task (mates send starts one)")
    pane = _live_pane(root, state, q)
    db = _db(root)
    if _busy(q, db):
        die(f"Qwen pane {a.n} is busy (OpenCode is replying); wait for it")
    if not input_empty(pane):
        die(f"Qwen pane {a.n} has text in its input box (Wai may be typing); not pasting over it")
    question = _unsafe(read_protocol, q["worktree"], "question.md")
    _unsafe(clear_protocol, q["worktree"])
    if question is not None or not a.sendback:
        task["questions"].append({"q": question, "a": text})
    now = time.time()
    if a.sendback:
        log_attempt(root, q, "sent-back")
        task["attempts"] += 1
        task["attempt_ts"] = now
        _clear_review(task)
    task["paste_ts"] = now
    save_state(root, state)
    paste(pane, _message(text))
    print(f"answered Qwen pane {a.n}" + (f" (sent back: attempt {task['attempts']})" if a.sendback else ""))
    if a.sendback:
        _remind(a.n, task)
    return 0


# --- check, diff ---------------------------------------------------------------------------

def _rmtree(path):
    """rmtree that also removes what a check made read-only. Only real folders are
    chmodded (lstat, never through a link): a link the check left must not make Wai's
    process change the mode of whatever it points at."""
    def unlock(d):
        with contextlib.suppress(OSError):
            os.chmod(d, 0o700)
        with contextlib.suppress(OSError), os.scandir(d) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False):
                    unlock(e.path)
    with contextlib.suppress(OSError):
        if stat.S_ISDIR(os.lstat(path).st_mode):
            unlock(path)
    shutil.rmtree(path, ignore_errors=True)


@contextlib.contextmanager
def _unpacked(root, n, tree, base):
    """`tree` written to a fresh Wai-private folder under the mates state folder, outside
    every worktree; removed after. No folder above it is Qwen's, so no file Qwen wrote
    (a `.mates/pytest.ini`, a conftest.py in a parent) can change the check; `qwen exec`
    mounts only this folder. `checkout-index`, not `git archive`, so a Qwen-edited
    `.gitattributes` export-ignore drops nothing."""
    stage = Path(tempfile.mkdtemp(prefix=f"{n}-", dir=fsutil.state_dir("checks")))
    try:
        with tempfile.TemporaryDirectory(prefix="mates-check-index-") as tmp:
            idx = Path(tmp) / "index"
            git_safe(root, n, "read-tree", tree, index=idx, attrs=base)
            git_safe(root, n, "checkout-index", "-a", "-f", f"--prefix={stage}/", index=idx, attrs=base)
        yield stage
    finally:
        _rmtree(stage)


def _check_args(args):
    if len(args) < 3 or args[1] != "--":
        die("usage: mates check N -- <cmd>")
    rest = args[2:]
    return _int(args[0]), rest[0] if len(rest) == 1 else shlex.join(rest)


def check(args):
    n, cmd = _check_args(args)
    root = _lead_root()
    state = _load(root)
    q = _qentry(state, n)
    db = _db(root)
    if _busy(q, db):
        die(f"Qwen pane {n} is busy (OpenCode is replying): Qwen could edit the files mid-check; wait for it")
    task = q.get("task")
    try:
        if task is None:  # Wai's own work with Qwen
            base = git_lead(root, "merge-base", "HEAD", f"refs/heads/{q['branch']}").stdout.strip()
            task = q["task"] = _task(base, direct=True)
        tree = snapshot(root, n, task["base"])
        env = {k: v for k, v in os.environ.items() if k not in ("QWEN_RUNTIME", "QWEN_ROOT", "QWEN_GIT_RO")}
        if state.get("runtime"):
            env["QWEN_RUNTIME"] = state["runtime"]
        with _unpacked(root, n, tree, task["base"]) as folder:
            env["QWEN_CHECK_DIR"] = str(folder)
            r = subprocess.run([session._qwen(), "exec", "--", "sh", "-c", cmd], cwd="/", env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except GitError as e:
        die(f"git failed on Qwen worktree {n}: {e}")
    except fsutil.UnsafeDir as e:
        die(f"refusing to check: {e}")
    out = r.stdout.decode("utf-8", "replace").splitlines()
    print(f"check exit {r.returncode} on tree {tree[:12]}")
    for line in out[-CHECK_TAIL:]:
        print(line)
    if r.returncode == 0:
        task.update(checked_tree=tree, check_cmd=cmd)
    else:
        task.update(checked_tree=None)
    save_state(root, state)
    return r.returncode


def diff(args):
    p = _parser("diff", "mates diff N")
    p.add_argument("n", type=int)
    a = p.parse_args(args)
    root = _lead_root()
    state = _load(root)
    q = _qentry(state, a.n)
    task = q.get("task")
    if not task:
        die(f"Qwen pane {a.n} has no open task (mates check N -- <cmd> opens one for Wai's own work)")
    base = task["base"]
    try:
        tree = snapshot(root, a.n, base)
        # --text: a binary-looking file still shows its bytes (attributes come from base anyway)
        full = git_safe(root, a.n, "diff", "--text", "--no-color", "--no-renames", base, tree, attrs=base).stdout
        stat_ = git_safe(root, a.n, "diff", "--no-color", "--no-renames", "--stat", base, tree, attrs=base).stdout
    except GitError as e:
        die(f"git failed on Qwen worktree {a.n}: {e}")
    hooks = git_lead(root, "config", "core.hooksPath", check=False).stdout.strip() or None
    if hooks and os.path.isabs(hooks) and Path(hooks).resolve().is_relative_to(Path(root)):
        hooks = str(Path(hooks).resolve().relative_to(Path(root)))  # an absolute path inside the repo
    flags = guard.guard_flags(full, task["files"], hooks_path=hooks,
                              runtime_files=runtime.runtime_files(root, base))
    sys.stdout.write(full)
    print(stat_.rstrip("\n"))
    print("flags: " + (", ".join(flags) if flags else "none"))
    if task.get("checked_tree") and task["checked_tree"] != tree:
        print(f"mates: this is not the checked snapshot ({task['checked_tree'][:12]}); run mates check {a.n} again",
              file=sys.stderr)
    task.update(diffed_tree=tree, flags=flags)
    save_state(root, state)
    return 0


# --- merge, sync, discard ---------------------------------------------------------------------

def _changed_paths(root, a, b):
    out = git_lead(root, "diff", "--name-only", "-z", "--no-renames", a, b).stdout
    return {p for p in out.split("\0") if p}


def _lead_dirty(root):
    """Paths with uncommitted changes in the lead checkout (untracked included)."""
    recs = git_lead(root, "status", "--porcelain=v1", "-z", "-uall").stdout.split("\0")
    paths, i = set(), 0
    while i < len(recs):
        rec = recs[i]
        if len(rec) > 3:
            paths.add(rec[3:])
            if rec[0] in "RC":  # a rename's source path follows
                i += 1
                if i < len(recs):
                    paths.add(recs[i])
        i += 1
    return paths


SUBJECT_MAX = 72


def commit_subject(n, summary):
    """`mates qwen-N: <first sentence of summary>`, cut at a word boundary to at most
    SUBJECT_MAX characters in all, with `…` when cut."""
    prefix = f"mates qwen-{n}: "
    text = " ".join((summary or "").split())
    text = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    room = SUBJECT_MAX - len(prefix)
    if len(text) > room:
        cut = text[:room - 1]
        if " " in cut and not text[room - 1].isspace():
            cut = cut.rsplit(" ", 1)[0]
        text = cut.rstrip(" ,;:-") + "…"
    return prefix + text


def merge(args):
    p = _parser("merge", 'mates merge N [-m "<commit message>"]')
    p.add_argument("n", type=int)
    p.add_argument("-m", dest="summary")
    a = p.parse_args(args)
    root = _lead_root()
    state = _load(root)
    q = _qentry(state, a.n)
    task = q.get("task")
    if not task or not task.get("checked_tree"):
        die(f"nothing checked on Qwen pane {a.n}: run mates check {a.n} -- <cmd> first")
    checked = task["checked_tree"]
    if task.get("diffed_tree") != checked:
        die(f"run mates diff {a.n} first: the lead must read the diff of exactly what merges")
    summary = a.summary.splitlines()[0] if a.summary else task.get("summary")
    if task.get("direct") and not a.summary:
        die(f"this is Wai's own work with Qwen: mates merge {a.n} -m \"<summary>\"")
    if git_lead(root, "symbolic-ref", "-q", "HEAD", check=False).returncode != 0:
        die("the lead checkout is not on a branch (detached HEAD); check out a branch first")
    base = task["base"]
    if _lead_head(root) != base:
        die(f"lead moved: run mates sync {a.n}, then check again")
    clash = sorted(_changed_paths(root, base, checked) & _lead_dirty(root))
    if clash:
        die("the lead checkout has uncommitted changes in paths this merge touches: " + ", ".join(clash[:10]))
    try:
        message = a.summary or commit_subject(a.n, summary)
        commit = git_lead(root, "commit-tree", checked, "-p", base, "-m", message).stdout.strip()
        git_lead(root, "merge", "--ff-only", "-q", commit, attrs=base)
    except GitError as e:
        die(f"merge failed: {e}", 1)
    task["summary"] = summary  # merged: log it and close the task before the worktree tidy-up
    log_attempt(root, q, "accepted")
    q["task"] = None
    save_state(root, state)
    print(f"merged Qwen pane {a.n}: {commit[:12]} {message.splitlines()[0]}")
    try:
        now_tree = snapshot(root, a.n, base)
        if now_tree != checked:
            ref = _keep(root, a.n, _snapshot_commit(root, a.n, base, now_tree, "edits after the check (not merged)"))
            print(f"mates: Qwen worktree {a.n} changed after the check; those edits are kept on {ref}", file=sys.stderr)
        _reset_worktree(root, a.n, q["branch"], commit)
    except GitError as e:
        print(f"mates: merged, but resetting Qwen worktree {a.n} failed ({e}); mates discard {a.n} tidies it",
              file=sys.stderr)
    return 0


def sync(args):
    p = _parser("sync", "mates sync N")
    p.add_argument("n", type=int)
    a = p.parse_args(args)
    root = _lead_root()
    state = _load(root)
    q = _qentry(state, a.n)
    task = q.get("task")
    if not task:
        die(f"Qwen pane {a.n} has no open task")
    base, lead = task["base"], _lead_head(root)
    if lead == base:
        print(f"nothing to sync: the lead is still at the task base {base[:12]}")
        return 0
    try:
        snap = _snapshot_commit(root, a.n, base, snapshot(root, a.n, base), "snapshot for sync")
        r = git_lead(root, "merge-tree", "--write-tree", "-z", "--name-only", "--no-messages",
                     f"--merge-base={base}", lead, snap, check=False, attrs=lead)
        if r.returncode == 1:
            paths = [x for x in r.stdout.split("\0")[1:] if x]
            print("conflict")
            for x in paths:
                print(x)
            return 1
        if r.returncode != 0:
            raise GitError(r.stderr.strip())
        merged = r.stdout.split("\0")[0].strip()
    except GitError as e:
        die(f"sync failed: {e}", 1)
    try:
        _reset_worktree(root, a.n, q["branch"], lead)
        _, admin, _ = gitsafe.dirs(root, a.n)
        with tempfile.TemporaryDirectory(prefix="mates-sync-index-") as tmp:
            idx = Path(tmp) / "index"
            shutil.copyfile(admin / "index", idx)  # matches the worktree, which is at `lead` now
            git_safe(root, a.n, "read-tree", "-m", "-u", lead, merged, index=idx, attrs=lead)
    except GitError as e:  # the worktree may be half-written now: Qwen's work must survive on a ref
        die(f"sync failed after the reset ({e}); Qwen's work is kept on {_keep(root, a.n, snap)}", 1)
    task["base"] = lead
    _clear_review(task)
    save_state(root, state)
    print(f"synced Qwen pane {a.n} onto {lead[:12]}; run mates check {a.n} -- <cmd> again")
    return 0


def keep_work(root, n, task, why="discarded"):
    """Keep Qwen worktree n's work (against the task base, else its HEAD) on a
    `refs/mates/discarded/` ref; returns the ref, or None when there is nothing."""
    base = task["base"] if task else git_safe(root, n, "rev-parse", "HEAD").stdout.strip()
    tree = snapshot(root, n, base)
    if tree == git_lead(root, "rev-parse", f"{base}^{{tree}}").stdout.strip():
        return None
    ref = _keep(root, n, _snapshot_commit(root, n, base, tree, why))
    print(f"kept Qwen worktree {n}'s work on {ref}")
    return ref


def discard(args):
    p = _parser("discard", "mates discard N [--failed]")
    p.add_argument("n", type=int)
    p.add_argument("--failed", action="store_true")
    a = p.parse_args(args)
    root = _lead_root()
    state = _load(root)
    q = _qentry(state, a.n)
    task = q.get("task")
    try:
        keep_work(root, a.n, task)
        _reset_worktree(root, a.n, q["branch"], _lead_head(root))
    except GitError as e:
        die(f"discard failed: {e}", 1)
    if task:
        log_attempt(root, q, "failed" if a.failed else "escalated")
        q["task"] = None
    save_state(root, state)
    print(f"discarded Qwen pane {a.n}'s task; its worktree is at the lead HEAD")
    return 0


def retro(args):
    import retro as r  # imported here: retro reads cli's constants at import
    return r.main(args)


COMMANDS = {"launch": launch, "retro": retro, "send": send, "wait": wait, "answer": answer, "check": check, "diff": diff,
            "merge": merge, "sync": sync, "discard": discard, "add": panes.add, "remove": panes.remove,
            "list": panes.list_, "log": panes.log, "log-route": panes.log_route}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        die("usage: cli.py <command> ...")
    cmd, args = argv[0], argv[1:]
    if cmd not in OPEN_COMMANDS and os.environ.get("MATES_LEAD") != "1":
        die(f"`mates {cmd}` runs only from the mates lead (MATES_LEAD=1)")
    if cmd not in COMMANDS:
        die(f"unknown command {cmd!r}")
    try:
        return COMMANDS[cmd](args)
    except GitError as e:
        die(f"git failed: {e}", 1)
    except RuntimeError as e:  # tmux failures (paste, display, capture-pane)
        die(str(e), 1)


if __name__ == "__main__":
    sys.exit(main())
