"""mates: Claude Code (the lead) on the left, sandboxed Qwen panes on the right.

`bin/mates` picks the tmux window and runs `cli.py launch` in the lead's pane:
that starts the Qwen panes, saves the state and prints the lead's `claude` argv
NUL-separated, which `bin/mates` runs as its child. The other subcommands are
the lead's tools and refuse unless MATES_LEAD=1 or the caller is a Claude Code session
(CLAUDECODE=1), except launch, list, log, retro and followers. `mates start` makes a Claude session that was
started as plain `claude` the lead.
"""
import argparse
import contextlib
import copy
import fcntl
import hashlib
import http.client
import json
import os
import re
import shlex
import signal
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
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
import usage
from gitsafe import GitError, git_lead, git_safe

IDLE_CLOSE_S = 1800
REAP_POLL_S = 300

# {worktree}: the pane's worktree, the same path inside the sandbox. OpenCode's write tool takes
# an absolute path, and Qwen once turned `.mates/question.md` into /mates/question.md, which
# mates never sees (spire 11-12).
ASK_SUFFIX = ("(If any name, file or expected result above is not stated exactly, write your question "
              "to {worktree}/.mates/question.md and stop before editing. Do not edit tests unless told.)")
HERE = Path(__file__).resolve().parent
LEAD_PROMPT = (HERE / "lead.md", HERE / "lead-lessons.md")
QWEN_LIVE = "@mates-qwen-live"  # set on a Qwen pane while its qwen runs (qwen_cmd)
OPEN_COMMANDS = {"launch", "list", "log", "retro", "followers"}  # the rest run only from a lead (see _is_lead)


def die(msg, code=2):
    print(f"mates: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _claude():
    return os.environ.get("MATES_CLAUDE") or "claude"


def _entry(root, n):
    return {"n": n, "worktree": str(Path(root) / ".claude" / "worktrees" / f"mates-qwen-{n}"),
            "branch": f"mates/qwen-{n}", "opencode_session": None, "closed": False, "task": None,
            "idle_since": None}


# --- the Qwen pane wrapper ----------------------------------------------------------

SANDBOX_VARS = ("QWEN_RUNTIME", "QWEN_PROOFS", "QWEN_CHECK_DIR", "QWEN_ROOT", "QWEN_GIT_RO", "QWEN_BRIEF")


def brief_dir(root, n):
    """The private folder (made if missing) holding pane n's brief for OpenCode's side panel
    (Wai, 2026-11-18: "show the overall plan and/or the instigating prompt in that Qwen panel").
    bin/qwen mounts it read-only for that pane only, never for a check."""
    return Path(fsutil.state_dir("brief", session.repo_key(str(root)), str(n)))


def _write_brief(root, n, text):
    """Pane n's panel shows `text` (the lead's brief, as sent) until the task ends; best
    effort: a folder that can't be made safe leaves the panel without it."""
    try:
        d = brief_dir(root, n)
    except (fsutil.UnsafeDir, OSError):
        return
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".brief-")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, d / "brief.md")


def _clear_brief(root, n):
    with contextlib.suppress(OSError, fsutil.UnsafeDir):
        (brief_dir(root, n) / "brief.md").unlink(missing_ok=True)


def sandbox_env(root, runtime, brief=None):
    """The sandbox variables every Qwen launch (pane or check) may set: the runtime and
    the proofs folder, only when they exist. The caller also clears SANDBOX_VARS first,
    so nothing inherited (a tmux server's environment, Wai's shell) enters the sandbox."""
    env = {}
    if brief:
        env["QWEN_BRIEF"] = str(brief)
    if runtime:
        env["QWEN_RUNTIME"] = str(runtime)
    proofs_dir = Path(root) / ".mates" / "proofs"
    if proofs_dir.is_dir():
        env["QWEN_PROOFS"] = str(proofs_dir)
    return env


def qwen_cmd(worktree, opencode_session=None, runtime_dir=None, root=None):
    """The command a Qwen pane runs. It waits for `release_pane` (so the pane is
    tagged before qwen can fail), then runs qwen in the worktree with read-only git,
    but only if the go signal came AND the pane's `@mates` tag is set; otherwise it
    says why and fails (the pane is marked failed), so no untagged qwen ever runs.
    While qwen runs the pane has `@mates-qwen-live=1` (`send` and `answer` need it:
    `#{pane_current_command}` would only ever show this wrapper's bash).
    Exit 0 closes the pane; any other exit marks it `@mates-failed=1` (the tag
    stays) and leaves a shell with the error. That shell runs as Wai, outside the
    sandbox, so it starts in the lead repo `root` (default: the worktree's repo),
    never among Qwen's files."""
    root = root or Path(worktree).parents[2]
    try:  # the panel's brief: panes only; a folder that can't be made safe just shows none
        brief = brief_dir(root, int(Path(worktree).name.rsplit("-", 1)[1]))
    except (fsutil.UnsafeDir, ValueError, IndexError, OSError):
        brief = None
    sets = {**sandbox_env(root, runtime_dir, brief), "QWEN_ROOT": str(worktree), "QWEN_GIT_RO": "1"}
    env = ["env", *(f"-u {k}" for k in SANDBOX_VARS), *(f"{k}={shlex.quote(v)}" for k, v in sets.items())]
    run = [*env, shlex.quote(session._qwen())]
    if opencode_session:
        run += ["-s", shlex.quote(opencode_session)]
    script = ('if ! timeout 30 tmux wait-for "mates-go-$TMUX_PANE"; then '
              'echo "mates: no go signal within 30s; qwen not started" >&2; rc=1; '
              'elif [ -z "$(tmux display -p -t "$TMUX_PANE" \'#{@mates}\')" ]; then '
              'echo "mates: this pane is not tagged; qwen not started" >&2; rc=1; '
              'else '
              f'cd {shlex.quote(str(worktree))} && tmux set-option -p -t "$TMUX_PANE" {QWEN_LIVE} 1 && '
              f'{" ".join(run)}; rc=$?; fi; '
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
        q1.update(closed=False, opencode_session=None)
        for q in st["qwens"]:
            q["task"] = None  # launch has kept each open task's work and logged it (_drop_open_tasks)
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
    st.setdefault("runtime", None)  # an older saved state may lack the key
    if runtime_dir:
        st["runtime"] = str(runtime_dir)
    elif st.get("runtime"):
        if not Path(st["runtime"]).exists():
            warnings.append(f"mates: saved runtime {st['runtime']} is gone; panes start without it "
                            "(the lead runs mates runtime --reason ... to rebuild)")
            st["runtime"] = None
        else:
            warnings.append(f"mates: keeping saved runtime {st['runtime']} (no built runtime given); "
                            "use .mates/runtime.sh + mates runtime --reason ... to make it stick after restarts")
    session_args = _claude_plan(st, root, restore, warnings)
    qwens = []
    for q in _qwen_entries(st, root, restore, warnings):
        q["runtime"] = st["runtime"]  # what this pane runs on (mates runtime restarts the stale ones)
        qwens.append({"n": q["n"], "worktree": q["worktree"],
                      "cmd": qwen_cmd(q["worktree"], q["opencode_session"], st["runtime"], root)})
    return {"claude_argv": _claude_argv(root, session_args, claude_args), "qwens": qwens,
            "warnings": warnings, "state": st}


def _pane_theme():
    """The lead's pane theme: MATES_PANE_THEME from bin/mates; else, when our parent is a bin/mates
    started before it named one (its /restart relaunch runs this launch), pane-<its pid>, made here
    so the statusline has a file to recolour."""
    theme = os.environ.get("MATES_PANE_THEME", "")
    if re.fullmatch(r"pane-\d+", theme):
        return theme
    ppid = os.getppid()
    try:
        args = Path(f"/proc/{ppid}/cmdline").read_bytes().split(b"\0")[:2]
    except OSError:
        return None
    if not any(a.endswith(b"/bin/mates") for a in args):
        return None
    theme = f"pane-{ppid}"
    f = Path.home() / ".claude" / "themes" / f"{theme}.json"
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        if not f.exists():
            f.write_text(json.dumps({"name": theme, "base": "dark", "overrides": {}}) + "\n")
    except OSError:
        return None
    return theme


def _claude_argv(root, session_args, claude_args):
    # Prompt bar = title colour: bin/mates names a theme of its own (MATES_PANE_THEME, like the
    # claude() wrapper in ~/.zshrc does), which ~/.claude/statusline-command.sh recolours.
    theme = _pane_theme()
    settings = ["--settings", json.dumps({"theme": f"custom:{theme}"})] if theme else []
    # MIMALLOC_PURGE_HOLES=0: Claude Code's Bun 1.4.4 segfaulted in its allocator's idle "purge
    # holes" step after hours (Spire, 2026-11-18, exit 139: mi_page_holes_madvisable from the
    # mimalloc scavenger); Bun's mimalloc 3.6.0 reads this switch, and it turns off only that step
    # (Wai said yes to trying it). bin/mates still resumes a lead that crashes.
    return ["env", "-u", "ANTHROPIC_API_KEY", "MATES_LEAD=1", f"MATES_ROOT={root}", "MIMALLOC_PURGE_HOLES=0",
            _claude(), *settings,
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
        with contextlib.suppress(RuntimeError):  # widths are cosmetic: keep the saved ones
            state["widths"] = layout.pane_widths(panes)
    session.save(root, state)


def snapshot(root, n, base):
    """The tree of Qwen worktree n as it is now: a temp index seeded with `base`, then
    every change outside `.mates/` added (untracked files too). Tracked `.mates/*`
    keep their `base` version; Qwen's edits there and untracked `.mates/*` never
    reach the tree. New objects go to a private quarantine store and reach the shared
    one (which Qwen panes can read) only after `_verify_snapshot` passes."""
    with tempfile.TemporaryDirectory(prefix="mates-snap-") as tmp:
        idx = Path(tmp) / "index"
        objs = Path(tmp) / "objects"
        objs.mkdir()
        q = {"index": idx, "attrs": base, "objects": objs}
        git_safe(root, n, "read-tree", base, **q)
        # Not `add -A -- . ':!.mates'`: git exits 1 when an excluded path is also ignored
        # (info/exclude lists .mates/). Add everything, then put .mates back as it was at base.
        git_safe(root, n, "add", "-A", "--", ".", **q)
        git_safe(root, n, "rm", "-r", "-f", "-q", "--cached", "--ignore-unmatch", "--", ".mates", **q)
        kept = git_safe(root, n, "ls-tree", "-r", "-z", "--full-tree", base, "--", ".mates", **q).stdout
        if kept:
            git_safe(root, n, "update-index", "-z", "--index-info", input=kept, **q)
        tree = git_safe(root, n, "write-tree", **q).stdout.strip()
        _verify_snapshot(root, n, base, tree, objs)
        gitsafe.promote(root, objs)
    return tree


def _read_nofollow(top, rel):
    """(mode, bytes) of worktree path `rel` with no link followed on the way: "120000" and
    the link text for a link, "file" and the content for a regular file. OSError otherwise."""
    *dirs, last = rel.split("/")
    fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for d in dirs:
            nfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
        st = os.stat(last, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISLNK(st.st_mode):
            return "120000", os.fsencode(os.readlink(last, dir_fd=fd))
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{rel} is not a regular file")
        ffd = os.open(last, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        with os.fdopen(ffd, "rb") as f:
            return "file", f.read()
    finally:
        os.close(fd)


def _verify_snapshot(root, n, base, tree, objects=None):
    """Refuse a snapshot that differs from the worktree read again with no link followed.
    `git add` runs as the owner while sandbox processes may still run: one that swaps a file
    for a link to an owner file while git reads it would put that file in the snapshot, then
    the diff, then main (security audit, 2026-11-13). Qwen can't read owner files, so it can't
    make the re-read match."""
    _, _, wt = gitsafe.dirs(root, n)
    out = git_safe(root, n, "diff-tree", "-r", "-z", "--no-renames", base, tree, attrs=base,
                   objects=objects).stdout
    fields = out.split("\0")
    for meta, path in zip(fields[0::2], fields[1::2], strict=False):
        if not meta.startswith(":"):
            continue
        _, mode, _, sha, status = meta[1:].split(" ")
        if status.startswith("D") or mode == "160000":
            continue
        try:
            kind, data = _read_nofollow(str(wt), path)
        except OSError as e:
            raise GitError(f"{path} changed while mates read it ({e}); not taking this snapshot") from e
        if (kind == "120000") != (mode == "120000"):
            raise GitError(f"{path} changed while mates read it (file and link swapped); not taking this snapshot")
        args = ("hash-object", "--no-filters", "--stdin") if kind == "120000" else ("hash-object", "--path", path, "--stdin")
        if git_safe(root, n, *args, input=data, attrs=base).stdout.decode().strip() != sha:
            raise GitError(f"{path} changed while mates read it; not taking this snapshot")


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
    """The runtime already built for HEAD's .mates/runtime.sh, or None. A launch never builds
    one: only `mates runtime --reason` runs an install (deliberate, and logged)."""
    try:
        if runtime.runtime_hash(root, "HEAD") is None:
            return None
        found = runtime.find_runtime(root, "HEAD")
    except (RuntimeError, OSError, fsutil.UnsafeDir) as e:
        warnings.append(f"mates: cannot read the runtime, panes start without it: {e}")
        return None
    if found is None:
        warnings.append("mates: .mates/runtime.sh is not built for HEAD; panes start without it "
                        "(the lead runs mates runtime --reason ...)")
        return None
    return str(found)


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


def _even_stack(lead, ids, warnings):
    """Even the heights of the Qwen panes `ids` ((n, pane), top to bottom) in their column;
    a warning (panes too small for OpenCode) is appended to `warnings`."""
    if not ids:
        return
    try:
        resizes, warning = layout.even_qwen_panes({lead: "lead", **{p: n for n, p in ids}})
    except RuntimeError as e:
        resizes, warning = [], f"mates: could not even out the Qwen panes: {e}"
    for argv in resizes:
        layout._tmux(*argv)
    if warning:
        warnings.append(warning)


def _split_qwens(root, lead, plan, warnings):
    """Step 4: worktree, split, tag, release, for each planned pane top to bottom. Each
    split halves the bottom pane, so the column is evened after every one: otherwise the
    halves run out of rows before the panes do."""
    ids = []
    for q in plan["qwens"]:
        hit = session.find_pane(root, q["n"])
        if hit is not None:
            warnings.append(f"mates: Qwen pane {q['n']} is already open ({hit[0]}); left as it is")
            continue
        if ids:  # one repo's OpenCode panes share a database: two starting at once race for it
            time.sleep(PANE_STAGGER_S)
        session.ensure_worktree(root, q["n"])
        try:
            pane = layout.split(layout.split_qwen(lead, [p for _, p in ids], q["worktree"], q["cmd"]))
        except layout.NoRoom:
            warnings.append(f"mates: no room for Qwen pane {q['n']} and after; widen the window and run mates again")
            break
        session.tag_pane(pane, root, q["n"])
        release_pane(pane)
        ids.append((q["n"], pane))
        if len(ids) > 1:
            _even_stack(lead, ids, [])
    return ids


def _is_lead():
    """A `mates`-launched lead (MATES_LEAD=1) or any Claude Code session (Claude Code sets
    CLAUDECODE=1 for its tools; Wai, 2026-11-16: "claude should be able to start mates
    regardless if started by mates or by claude"). Not Wai's shell, not Qwen's OpenCode."""
    return os.environ.get("MATES_LEAD") == "1" or os.environ.get("CLAUDECODE") == "1"


def _claude_pid():
    """The pid of the claude process this command runs under (the nearest ancestor named
    claude), or None."""
    pid = os.getppid()
    while pid > 1:
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
            stat_ = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return None
        if comm == "claude":
            return pid
        pid = int(stat_.rsplit(")", 1)[1].split()[1])  # field 4, after the (comm)
    return None


def followers(args):
    """Whether Claude followers may run now (usage.py): prints why; exit 0 on, 1 off."""
    _parser("followers", "mates followers").parse_args(args)
    h = usage.read()
    print(usage.report(h))
    return 0 if h["on"] else 1


def start(args):
    """Make this Claude session (started as plain `claude`, in tmux) the repo's mates lead:
    tag its pane, mark its window, save a state if there is none, start the idle-pane
    reaper, and print the lead prompt (lead.md, the lessons) for the session to follow."""
    _parser("start", "mates start").parse_args(args)
    pane = os.environ.get("TMUX_PANE") or die("mates needs tmux: start claude inside a tmux pane, then mates start")
    root = _lead_root()
    lead = session.find_lead(root)
    if lead and lead != pane:
        die(f"this repo already has a mates lead in tmux pane {lead}: work from that session, or close it first")
    claude = _claude_pid()
    if claude is None:
        die("mates start runs from a Claude Code session (no claude process above this one)")
    state = session.load(root) or session.new_state(root)
    session.tag_pane(pane, root, "lead")
    # bin/mates treats the lead as live while this claude runs, so it never restarts the pane
    # under the session (it would otherwise read a lead it did not start as one that exited)
    for argv in (("set-option", "-p", "-t", pane, "@mates-lead-pid", str(claude)),
                 ("set-option", "-w", "-t", pane, "@mates-window", session.repo_key(root))):
        r = subprocess.run([layout.TMUX, *argv], capture_output=True, text=True)
        if r.returncode != 0:
            die(f"tmux {' '.join(argv[:2])} {argv[4]} failed: {r.stderr.strip()}", 1)
    save_state(root, state)
    start_reaper(root)
    print(_lead_prompt())
    print(f"\nmates: this session is now the mates lead for {root}. Open a Qwen pane with `mates add`; "
          "the rules above apply from here on.")
    return 0


def _drop_open_tasks(root, old, warnings):
    """`launch --fresh` forgets every open task: keep each one's work on a ref and log it as
    escalated first, as discard does, so no task vanishes without a line (2026-11-17 review).
    launch's stdout is the lead's argv, so keep_work's message goes to stderr."""
    for q in old["qwens"]:
        task = q.get("task")
        if not task:
            continue
        try:
            if Path(q["worktree"]).exists():
                with contextlib.redirect_stdout(sys.stderr):
                    keep_work(root, q["n"], task)
            log_attempt(root, dict(q), "escalated")
        except (GitError, Unsafe, OSError, RuntimeError, subprocess.CalledProcessError) as e:
            warnings.append(f"mates: could not keep or log Qwen pane {q['n']}'s open task {task.get('id')} ({e})")


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
            _drop_open_tasks(root, old, warnings)
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
        layout.restore_widths(lead, state.get("widths") or [])
        _even_stack(lead, ids, warnings)
    for w in warnings:
        print(w, file=sys.stderr)
    session.tag_pane(lead, root, "lead")
    r = subprocess.run([layout.TMUX, "set-option", "-w", "-t", lead, "@mates-window", session.repo_key(root)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        die(f"tmux set-option @mates-window failed: {r.stderr.strip()}", 1)
    save_state(root, state)
    start_reaper(root)  # closes Qwen panes unused for 30 minutes (one per repo, under a lock)
    sys.stdout.buffer.write(b"".join(a.encode() + b"\0" for a in argv))
    sys.stdout.flush()
    return 0


# --- delegation: helpers ---------------------------------------------------------------

LOG = Path.home() / ".claude" / "mates" / "log.jsonl"
TEXT_MAX = 2000  # Qwen's done/question text is cut here
WAIT_DEFAULT_S = 1800
WAIT_POLL_S = 2.0
STALL_S = 1800  # a pane that reads busy while OpenCode has written nothing for this long is stalled
SESSION_WAIT_S = 30  # send: how long to look for the OpenCode session row after the message
SESSION_POLL_S = 0.5
CHECK_TAIL = 20
CHECK_TIMEOUT_S = 1800  # a check still running after this is killed and fails ("timed out")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def shown(text):
    """Text Qwen wrote, safe to print for the lead: control characters (escape sequences,
    carriage returns, bells) become "?", so nothing rewrites the terminal or hides text."""
    return _CONTROL.sub("?", text)
# Task 0's input-box rule, read from OpenCode 1.19.5's TUI: an empty prompt shows the
# placeholder `Ask anything… "<example>"` (`Run a command… "…"` in shell mode, which is
# not empty input); typed text replaces it. Looked for on every visible line: a fresh
# session draws the box mid-pane, under the logo (seen in Wai's first real run). The
# opening quote of the example is part of the rule, so chat text that merely says
# "Ask anything..." does not count as an empty box.
INPUT_EMPTY = re.compile(r'Ask anything(…|\.\.\.) "')
# The footer while a reply runs: "esc interrupt", then "esc again to interrupt" once one Escape
# has armed the interrupt (the reply goes on until a second press).
ESCAPE_INTERRUPT = re.compile(r'esc\s+(?:again\s+to\s+)?interrupt')
SPIN_GLYPHS = re.compile(r"[⬝■]{4,}")
ESCAPE_AGAIN = re.compile(r'esc\s+again\s+to\s+interrupt')
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
        die("no mates state for this repo (run mates start from this Claude session, or start it with mates)")
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


def _change(root, n, fn, task_id=None):
    """Change entry n, and only it, under the state lock on the saved state as it is now.
    A command that works for minutes holds an old copy of the state: writing that copy back
    undid what other commands did meanwhile (2026-11-17 reviews), so a command writes only
    the fields it owns through here. With `task_id`, fn runs only while the entry's task is
    still that one."""
    def apply(st):
        e = next((x for x in st.get("qwens", []) if x["n"] == n), None)
        if e is None or (task_id is not None and (e.get("task") or {}).get("id") != task_id):
            return
        fn(e)
    session.update(root, apply)


def _find(root, state, q):
    """(pane_id, status) for entry q; a pane Wai closed is marked closed, in `state` and
    in the saved entry only (session.mark_closed: never while the lead pane is gone too)."""
    hit = session.find_pane(root, q["n"])
    if hit is None and session.mark_closed(state, q["n"]):
        session.update(root, lambda st: session.mark_closed(st, q["n"]))
    return hit


def _live_pane(root, state, q):
    """The pane id of entry q, refusing unless OpenCode runs in it now."""
    if q.get("closed"):
        if q.get("closed_idle"):
            die(f"Qwen pane {q['n']} was closed after 30 minutes unused (mates add opens it again)")
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


PASTE_GRACE_S = 30  # after mates' own paste, the spinner takes a moment: the database counts
# A reply mid-step with no write for this long no longer reads busy without the spinner. A
# request can wait for the one shared model slot up to OpenCode's headerTimeout (3600 s in
# bin/qwen) writing nothing (2026-11-17: 30 min of queueing read as idle), so this is past it.
MID_REPLY_S = 3900
PANE_STAGGER_S = 5  # a launch starts a repo's panes this far apart (OpenCode's database race)
QWEN_TASK_CAP = 3  # at most this many open Qwen tasks machine-wide


def _open_qwen_tasks():
    """`<repo folder>:<n>` of every open lead task (not direct, pane not closed) in every
    repo's saved state."""
    out = []
    for f in sorted(Path(session.STATE_DIR).glob("*.json")):
        try:
            st = json.loads(f.read_text())
            name = Path(st["root"]).name
            out += [f"{name}:{q['n']}" for q in st.get("qwens") or []
                    if q.get("task") and not q["task"].get("direct") and not q.get("closed")]
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


def _box_border(lines):
    """Index of the input box's bottom border (`╹▀▀▀`, the last one on screen), or None."""
    return max((i for i, line in enumerate(lines) if line.lstrip().startswith("╹▀")), default=None)


def _footer(lines, border):
    """The footer under the input box, its lines joined: in a narrow pane the spinner wraps
    across two. Only the footer counts, so chat text that quotes the words does not."""
    return " ".join(lines[border + 1:] if border is not None else lines[-3:])


# OpenCode's side panel (session title, context, cost) stays open (Wai, 2026-11-18: "always have
# the right pane open for Qwen like now, it should take priority over the chat window / ctrl+x b,
# is the shortcut to open the panel if the pane is ever resized too small to have both"). With the
# panel hidden, OpenCode 1.19.5 moves the context figure ("37.9K (5%)") into the footer. But a
# wide, short pane (168x26, 2026-11-18) shows the figure there with the panel open too, and then
# the panel's own last line, "• OpenCode 1.19.5", sits at the end of the same row: reading that
# as hidden pressed Ctrl+X B on every poll, and the panel flickered open and shut.
PANEL_HIDDEN = re.compile(r"\d+(?:\.\d+)?K \(\d")
PANEL_OPEN = re.compile(r"• OpenCode \d")


def _panel_hidden(footer):
    return bool(PANEL_HIDDEN.search(footer)) and not PANEL_OPEN.search(footer)


def ensure_panel(pane, lines):
    """Press Ctrl+X B when a chat shows with its side panel hidden; True if it pressed. The
    start screen (no chat yet) has no panel and is left alone."""
    border = _box_border(lines)
    if border is None or not _panel_hidden(_footer(lines, border)):
        return False
    _tmux_out("send-keys", "-t", pane, "C-x", "b")
    return True


def _spinner(lines, border):
    """OpenCode's spinner footer: its progress glyphs (a run of ⬝ and ■; the idle footer shows
    the folder there), or the words "esc interrupt" / "esc again to interrupt". The glyphs
    count on their own because a narrow pane wraps the words mid-word beside other columns
    (2026-11-17, 63 columns: "esc" / "interr" / "upt")."""
    footer = _footer(lines, border)
    return bool(SPIN_GLYPHS.search(footer) or ESCAPE_INTERRUPT.search(footer))


def _screen(pane):
    return _tmux_out("capture-pane", "-p", "-t", pane).splitlines()


def screen_busy(pane):
    """True when the spinner shows: OpenCode is working whatever the database says."""
    lines = _screen(pane)
    return _spinner(lines, _box_border(lines))


def _pane_started(pane):
    """When the process now in `pane` started (epoch s), or 0 when that cannot be read."""
    try:
        pid = int(_tmux_out("display", "-p", "-t", pane, "#{pane_pid}").strip())
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        boot = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines()
                    if line.startswith("btime "))
        return boot + int(fields[19]) / os.sysconf("SC_CLK_TCK")  # field 22, starttime
    except (RuntimeError, ValueError, OSError, IndexError, StopIteration):
        return 0


def busy(q, db, pane, since=0, sid=None, lines=None):
    """The one busy rule (send, answer, check, wait). The spinner on screen means busy.
    Without it, with the input box on screen and mates' last paste over PASTE_GRACE_S old,
    busy means the chat is mid-reply in the database (session.opencode_mid_reply) by the
    OpenCode now in the pane and within MID_REPLY_S: a screen read can miss the spinner
    (human-music, 2026-11-16: wait said idle mid-reply), while a killed pane's unfinished
    reply or a stray write to an old session must not read busy. Right after a paste, or
    with no box on screen, the database rule decides (messages since `since`). `lines`: a
    capture of the pane just taken."""
    lines = _screen(pane) if lines is None else lines
    border = _box_border(lines)
    if _spinner(lines, border):
        return True
    paste = (q.get("task") or {}).get("paste_ts")
    if sid is None:
        sid = session.opencode_session(q["worktree"], db)
    if border is not None and not (paste and time.time() - paste < PASTE_GRACE_S):
        last = session.opencode_mid_reply(sid, db)
        return last is not None and time.time() - last < MID_REPLY_S and last >= _pane_started(pane)
    if sid is None and not since:
        return False  # no session yet and no paste to wait for
    return session.opencode_state(sid, db, since) == "busy"


def input_text(pane):
    """The text in the OpenCode input box of `pane`: "" when empty, else the ┃ rows above
    the box's status row (which sits on the box's bottom border), stripped and joined by
    newlines; None when the screen cannot be read as a box. A fresh session shows the
    placeholder (empty). A used one shows only blank rows. Past user messages higher up
    share the ┃ gutter but end at a blank line."""
    lines = _tmux_out("capture-pane", "-p", "-t", pane).splitlines()
    if any(INPUT_EMPTY.search(line) for line in lines):
        return ""
    border = _box_border(lines)
    # Only the box's own columns: a wide pane prints a side column (path, branch) past the
    # end of the border.
    end = len(lines[border].rstrip()) if border is not None else 0
    if border is None or border < 1 or not lines[border - 1][:end].lstrip().startswith("┃"):
        return None
    rows = []
    for line in reversed(lines[:border - 1]):  # up from the status row
        s = line[:end].lstrip()
        if not s.startswith("┃"):
            break
        rows.append(s[1:].strip())
    if not rows:
        return None
    return "\n".join(reversed(rows)).strip()


def input_empty(pane):
    """True when the OpenCode input box in `pane` holds no typed text. An unreadable screen
    reads as not empty (fail closed: never paste over what Wai is typing)."""
    return input_text(pane) == ""


BOX_TEXT_SHOWN = 2000  # characters of the box's text a refusal prints
BSPACE_MARGIN = 20  # extra Backspaces past the text: the rows may hide spaces and newlines
BSPACE_CHUNK = 200  # keys per tmux send-keys call


# A chat squeezed by OpenCode's side panel (Wai, 2026-11-18: "close pane, send work, then reopen
# pane"): the panel keeps about 44 columns and the chat shrinks, and under CHAT_MIN columns the
# input box cannot be read (its placeholder is cut and reads as typed text). _box_guard then
# closes the panel (Ctrl+X B) for this command; send and answer reopen it when they finish.
CHAT_MIN = 30
PANEL_REDRAW_S = 0.6
_PANELS_CLOSED = set()


def _chat_squeezed(lines):
    border = _box_border(lines)
    if border is None or _panel_hidden(_footer(lines, border)):
        return False  # no chat on screen, or its panel is already hidden
    return len(lines[border].strip()) < CHAT_MIN


def _focus_lead():
    """Make the lead the active pane of its window: the tmux tab shows the active pane's title
    (Wai, 2026-11-18: "after using Qwen pane, always refocus Leader pane so that the tmux titles
    are accurate"). It never switches the window Wai is looking at."""
    with contextlib.suppress(Exception):
        lead = session.find_lead(_lead_root())
        if lead:
            _tmux_out("select-pane", "-t", lead)


def _reopening_panels(fn):
    """send and answer: reopen any panel _box_guard closed, then refocus the lead, however the
    command ends."""
    def run(args):
        try:
            return fn(args)
        finally:
            for pane in sorted(_PANELS_CLOSED):
                with contextlib.suppress(RuntimeError):
                    _tmux_out("send-keys", "-t", pane, "C-x", "b")
            _PANELS_CLOSED.clear()
            _focus_lead()
    run.__name__, run.__doc__ = fn.__name__, fn.__doc__
    return run


def _box_guard(n, pane, clear):
    """Die unless the input box of Qwen pane n is empty. With `clear`, delete its text first
    (Backspace presses, as a typist would: OpenCode's kill-line and select-all keys are
    version-dependent and Ctrl-C quits an empty prompt, while Backspace works the same on
    every version and the e2e fake) and refuse if it is still not empty. Without, refuse
    with the text, so the lead can carry what matters and run again with --clear-box.
    A chat squeezed by the side panel gets the panel closed first (send and answer reopen it)."""
    if pane not in _PANELS_CLOSED and _chat_squeezed(_screen(pane)):
        _tmux_out("send-keys", "-t", pane, "C-x", "b")
        _PANELS_CLOSED.add(pane)
        time.sleep(PANEL_REDRAW_S)
    text = input_text(pane)
    if text == "":
        return
    if text is not None and clear:
        left = len(text) + BSPACE_MARGIN
        while left > 0:
            k = min(left, BSPACE_CHUNK)
            _tmux_out("send-keys", "-t", pane, *["BSpace"] * k)
            left -= k
        if input_empty(pane):
            return
        die(f"Qwen pane {n} still has text in its input box after --clear-box; not pasting over it")
    if text is None:
        die(f"Qwen pane {n}: the input box cannot be read (not pasting over what may be typed); try again")
    die(f"Qwen pane {n} has text in its input box (Wai may be typing); not pasting over it. "
        f"Its text:\n{shown(text)[:BOX_TEXT_SHOWN]}\n"
        "Carry anything that matters into your own message, then run the same command again "
        "with --clear-box to delete the text and send. Do not wait on it.")


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


def _message(text, worktree):
    return f"[from Claude] {text}\n{ASK_SUFFIX.format(worktree=worktree)}"


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
    hit = read_protocol_file(wt, name)
    return None if hit is None else hit[0]


def read_protocol_file(wt, name):
    """(text, mtime) of `.mates/<name>` (text cut at TEXT_MAX), or None if there is none."""
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
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise Unsafe(f".mates/{name} is not a plain file")
            data = os.read(fd, TEXT_MAX * 4)
        finally:
            os.close(fd)
    finally:
        os.close(d)
    return data.decode("utf-8", "replace").strip()[:TEXT_MAX], st.st_mtime


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
            "questions": [], "updates": [], "diffed_tree": None, "flags": [], "checked_tree": None, "check_cmd": None}


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
    tokens = compactions = last = None
    try:  # every session in the worktree: a pane restart or a lost session id still counts
        tokens, compactions, last = session.opencode_usage(entry["worktree"], session.opencode_db(root),
                                                           t["attempt_ts"])
    except (OSError, subprocess.CalledProcessError, sqlite3.Error) as e:
        print(f"mates: cannot read Qwen's tokens: {e}", file=sys.stderr)
    # finish: Qwen's last reply in this attempt; with none (Wai's own work), now
    rec = {"worker_wall_s": round(max(0.0, (last or now) - t["attempt_ts"]), 1), "worker_tokens": tokens,
           "compactions": compactions, "answered_by": answered_by()}
    state = session.load(root) or {}
    # the lead's work on this attempt: since the send (lead_since is set there), split by kind
    rec.update(session.lead_cost_fields(session.transcript_path(root, state), entry["n"],
                                        entry.get("lead_since") or 0))
    entry["lead_since"] = now  # the next attempt's lead tokens start here (the caller saves)
    return rec


def log_attempt(root, entry, outcome, qwen_share=None):
    """Append the log line for the attempt on entry's task that just finished. `qwen_share`:
    the share (0-100) of the task Qwen completed, weighed by difficulty, when the task ends."""
    t = entry["task"]
    checked = t.get("checked_tree")
    # a review clears check_cmd (sendback, sync): the row then names the check the task was sent with
    # (the retro for 11-18: sent-back and escalated rows logged check: null while send_check was set)
    check = t.get("check_cmd") or t.get("send_check")
    rec = {"repo": str(root), "task_id": t["id"], "summary": t.get("summary") or "", "route": "qwen",
           "reason": t.get("reason"), "attempts": t.get("attempts", 0), "outcome": outcome, "pane": entry["n"],
           "check": check, "send_check": t.get("send_check"),
           "check_sha": hashlib.sha256(check.encode()).hexdigest() if check else None,
           "tree": checked, "flags": t.get("flags") or [],
            "full_diff_read": checked is not None and t.get("diffed_tree") == checked,
            "red_on_base": t.get("red_on_base"),
            "questions": t.get("questions") or [], "updates": t.get("updates") or [], **_costs(root, entry, t)}
    if qwen_share is not None:
        rec["qwen_share"] = qwen_share
    if not rec["flags"] and outcome != "accepted" and t.get("base"):
        # An attempt that ends before `mates diff` ran (sent back, escalated, failed) still gets its
        # guard flags, from the worktree as it is (the retro for 11-17: one left 4 scratch files at
        # the repo root and logged flags: []; Wai said yes to this). Never fails the log line.
        with contextlib.suppress(Exception):
            rec["flags"] = _snapshot_diff(root, entry["n"], t)[4]
    logrec.write(LOG, rec)


MAX_ATTEMPTS = 2  # lead.md: two failed attempts, then the lead does the task itself


def _remind(n, task):
    if task["attempts"] >= MAX_ATTEMPTS:
        print(f"mates: this is attempt {task['attempts']} on this task, the last; if it fails, "
              f"mates discard {n} --share P and do it yourself", file=sys.stderr)


def _parser(name, usage):
    return argparse.ArgumentParser(prog=f"mates {name}", usage=usage)


# --- send, wait, answer ------------------------------------------------------------------

def cap_lock_path():
    """The one lock all leads on this machine take to count and reserve a Qwen task slot."""
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "mates" / "task-cap.lock"


@contextlib.contextmanager
def _cap_lock():
    path = cap_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _stamp_idle(root, n):
    """Restart the pane's idle clock now, so the reaper does not close it under a command."""
    _change(root, n, lambda e: e.update(idle_since=time.time()))


def _send_guards(a, root, state, q, db, pane, cap):
    """The refusals that must hold when send acts: the pane is not replying, is wide enough, has an
    empty input box, no other pane's task blocks it, and the worktree has no unmerged work. Send runs
    them before its base check (up to 30 minutes) and again after it."""
    if busy(q, db, pane):
        die(f"Qwen pane {a.n} is busy (OpenCode is replying); wait for it")
    # At most QWEN_TASK_CAP open Qwen tasks machine-wide (Wai, 2026-11-13: "A" = 3; the one model
    # slot made each reply wait ~7.5 min with 5 panes). Open: a lead's task (not Wai's direct
    # work) on a pane not closed, in any repo's saved state. Re-sending an open task is free.
    if cap and (not q.get("task") or q["task"].get("direct")):
        holding = _open_qwen_tasks()
        if len(holding) >= QWEN_TASK_CAP:
            die(_cap_message(holding))
    _refuse_narrow(a.n, pane)
    _box_guard(a.n, pane, a.clear_box)
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


def _cap_message(holding):
    return (f"the Qwen task cap is reached ({QWEN_TASK_CAP} open tasks: {', '.join(holding)}); "
            "merge or discard one first")


@_reopening_panels
def send(args):
    p = _parser("send", 'mates send N --reason "<why Qwen>" [--files a,b] [--continue] [--check "<cmd>"] '
                        '[--summary "<line>"] [--clear-box] "<text>"')
    p.add_argument("n", type=int)
    p.add_argument("--reason", required=True)
    p.add_argument("--summary", default=None)
    p.add_argument("--files", default="")
    p.add_argument("--continue", dest="cont", action="store_true")
    p.add_argument("--check", default=None)
    p.add_argument("--clear-box", action="store_true")
    p.add_argument("text", nargs="+")
    a = p.parse_args(args)
    text = " ".join(a.text)
    if a.summary is not None and not 0 < len(a.summary) <= 120:
        die("--summary must be one line of 1 to 120 characters")
    root = _lead_root()
    state = _load(root)
    q = _qentry(state, a.n)
    pane = _live_pane(root, state, q)
    _stamp_idle(root, a.n)  # before the base check, which can take 30 minutes
    db = _db(root)
    _send_guards(a, root, state, q, db, pane, cap=True)
    lead = _lead_head(root)
    if a.check:
        try:
            tree = git_lead(root, "rev-parse", f"{lead}^{{tree}}").stdout.strip()  # the lead's files: the worktree may be behind
            code, err = run_check(root, state, a.n, tree, lead, a.check)
            err = err.strip()
        except GitError as e:
            die(f"git failed on Qwen worktree {a.n}: {e}")
        except fsutil.UnsafeDir as e:
            die(f"refusing to check: {e}")
        if code == 0:
            die("the check passes on the base (it proves nothing); fix the check; with no red check, do it yourself")
        if code in (124, 126, 127) or "command not found" in err or "Executable doesn't exist" in err:
            die(f"the check cannot run on the base ({err[-2000:]}); fix the check; with no red check, do it yourself")
        # pytest exit 4 (usage error, e.g. a file not found) and 5 (no tests collected), or their
        # messages: the check names a test that is not in the lead's commit, so it is not red on
        # the base, it does not exist there (and Qwen would write the test that defines done).
        if code in (4, 5) or "file or directory not found" in err or "no tests ran" in err:
            die(f"the check cannot run on the base: it names a test that is not there ({err[-2000:]}); "
                "the test file must be committed first; with no red check, do it yourself")
        print(f"check {a.check} failed on the base (exit {code}) — red on base; sending anyway")
        # Minutes passed: the pane may be gone, busy or typed in, and the worktree edited. Look
        # again, on the state as it is now, before anything is reset or pasted.
        state = _load(root)
        q = _qentry(state, a.n)
        pane = _live_pane(root, state, q)
        _stamp_idle(root, a.n)
        _send_guards(a, root, state, q, db, pane, cap=False)
    # Qwen cannot commit, so mates/qwen-N holds no work of its own: moving it to the lead
    # HEAD is the rebase. Anything on it the lead lacks is kept on a ref first.
    head = git_safe(root, a.n, "rev-parse", "HEAD").stdout.strip()
    if git_lead(root, "merge-base", "--is-ancestor", head, lead, check=False).returncode != 0:
        print(f"mates: {q['branch']} had commits the lead lacks; kept on {_keep(root, a.n, head)}", file=sys.stderr)
    _reset_worktree(root, a.n, q["branch"], lead)
    _unsafe(clear_protocol, q["worktree"])
    files = [f for f in a.files.split(",") if f]
    summary = a.summary or text.splitlines()[0][:120]
    before = session.opencode_session(q["worktree"], db)
    task = _reserve_task(root, a, q, lead, summary, files, before)
    if not a.cont:
        paste(pane, "/new")  # a clean context per task (Task 0: works; the row comes with the message)
    _patch_task(root, a.n, task["id"], paste_ts=time.time())
    sent_text = text
    if a.check:
        sent_text = text + "\n\nCheck: " + a.check
    paste(pane, _message(sent_text, q["worktree"]))
    _write_brief(root, a.n, sent_text)  # shown at the top of OpenCode's side panel
    sid = task["opencode_session"]
    if sid is None:
        deadline = time.time() + SESSION_WAIT_S
        while True:
            found = session.opencode_session(q["worktree"], db)
            if found and found != before:
                sid = found
                _patch_task(root, a.n, task["id"], opencode_session=sid)
                break
            if time.time() >= deadline:
                print(f"mates: no new OpenCode session for pane {a.n} yet; mates wait keeps looking", file=sys.stderr)
                break
            time.sleep(SESSION_POLL_S)
    print(f"sent to Qwen pane {a.n} (task {task['id']}, attempt {task['attempts']})")
    _log_sent(root, a.n, task, "send", sent_text)
    _log_started(root, a.n, task)
    _remind(a.n, task)
    return 0


def _log_started(root, n, task):
    """The "started" row for a task just sent; the row that closes it (log_attempt) has the same task_id."""
    check = task.get("send_check")
    logrec.write(LOG, {"repo": str(root), "task_id": task["id"], "summary": task.get("summary") or "",
                       "route": "qwen", "reason": task.get("reason"), "attempts": task.get("attempts", 0),
                       "outcome": "started", "pane": n, "send_check": check})


def sent_path(root, day):
    """mates' own record of the briefs and answers pasted to `root`'s Qwen panes on UTC `day`:
    the daily retro reads this, never Qwen's OpenCode database, which Qwen can write (Wai,
    2026-11-18: "2A"). LOG is read when called, so tests' isolation covers it."""
    return LOG.parent / "sent" / session.repo_key(str(root)) / f"{day}.jsonl"


def _log_sent(root, n, task, kind, text):
    """Append one line (owner-only file and folders) to the record for this UTC day."""
    now = time.gmtime()
    path = sent_path(root, time.strftime("%Y-%m-%d", now))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    os.chmod(path.parent.parent, 0o700)
    if not path.exists():
        path.touch(mode=0o600)
    line = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", now),
        "repo": str(root),
        "n": n,
        "task_id": task["id"],
        "kind": kind,
        "text": text,
    }
    with path.open("a") as f:
        f.write(json.dumps(line) + "\n")


def _reserve_task(root, a, q, lead, summary, files, before):
    """Write the task into pane a.n's saved entry and return it: a new one, or the open one sent
    again (its last attempt logged as sent-back). A new task takes its slot under the machine-wide
    cap lock, counted again there: two leads sending at once each passed the count made minutes
    earlier (2026-11-17 review). Only this entry's task, opencode_session and lead_since are written."""
    def write(entry, task):
        def apply(e):
            e["task"] = task
            e["lead_since"] = task["attempt_ts"]  # this attempt's lead work counts from the send
            if before:
                e["opencode_session"] = before
        _change(root, a.n, apply)
        return task

    def attach(task):
        if a.cont:
            task["opencode_session"] = before
        if a.check:
            task.update(check_cmd=a.check, send_check=a.check, red_on_base=True)
        return task

    entry = _qentry(_load(root), a.n)  # the entry as it is now
    old = entry.get("task")
    if old and not old.get("direct"):  # the same task sent again: its last attempt is over
        log_attempt(root, entry, "sent-back")
        now = time.time()
        old.update(summary=summary, reason=a.reason, files=files, base=lead, attempts=old["attempts"] + 1,
                   attempt_ts=now, paste_ts=now)
        _clear_review(old)
        return write(entry, attach(old))
    with _cap_lock():
        holding = _open_qwen_tasks()
        if len(holding) >= QWEN_TASK_CAP:
            die(_cap_message(holding))
        return write(entry, attach(_task(lead, summary=summary, reason=a.reason, files=files)))


def _patch_task(root, n, task_id, **fields):
    """Set fields on pane n's task in the saved state, if it is still task_id."""
    def apply(e):
        e["task"].update(fields)
        if "opencode_session" in fields:
            e["opencode_session"] = fields["opencode_session"]
    _change(root, n, apply, task_id=task_id)


def _quiet_s(worktree, db, since):
    """Seconds since OpenCode last wrote in any session of `worktree`, counted from `since` (the
    paste) at the earliest; 0 when there is no paste time and nothing was written."""
    last = session.last_activity(worktree, db)
    return 0.0 if last is None and not since else time.time() - max(last or 0, since)


def wait_lock_path(root, n):
    """The lock one `mates wait` holds on pane n of this repo while it waits."""
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "mates" / f"wait-{session.repo_key(str(root))}-{n}.lock"


def wait(args):
    p = _parser("wait", "mates wait N [--timeout S]")
    p.add_argument("n", type=int)
    p.add_argument("--timeout", type=float, default=WAIT_DEFAULT_S)
    a = p.parse_args(args)
    root = _lead_root()
    # One wait per pane (Wai, 2026-11-18: leads opened "a bunch of shells", two waits on one
    # pane in the same minute). A second one steps aside at once with exit 0, so no rerun loop
    # spins on it; the lock goes with this process.
    lock = wait_lock_path(root, a.n)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock_f = open(lock, "w")  # noqa: SIM115 (held for the life of this wait)
    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"already-waiting: another mates wait {a.n} is running on this pane; use its result")
        return 0
    state = _load(root)
    q = _qentry(state, a.n)
    task = q.get("task")
    since = (task.get("paste_ts") or task["attempt_ts"]) if task else time.time()
    db = _db(root)
    deadline = time.time() + a.timeout
    stopped = 0
    while True:
        hit = session.find_pane(root, a.n)
        if hit is None:  # mark it closed in the state as it is now (wait may have run for a while)
            session.update(root, lambda st: session.mark_closed(st, a.n))
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
        # The one busy rule; a stop counts only when it holds on two polls in a row: one
        # capture can miss the spinner (OpenCode mid-redraw) while the database is quiet for
        # minutes between steps on the shared slot.
        # A protocol file counts once OpenCode is not replying right now (check's busy rule):
        # Qwen writes done.md and then keeps replying for a while, and check refuses meanwhile.
        # Without one, idle means no reply is running since the paste.
        found = [(word, name, *f) for name, word in (("question.md", "question"), ("done.md", "done"))
                 if (f := _unsafe(read_protocol_file, q["worktree"], name)) is not None]
        lines = _screen(hit[0])
        ensure_panel(hit[0], lines)  # Wai: OpenCode's side panel stays open
        still = busy(q, db, hit[0], lines=lines) if found else busy(q, db, hit[0], since, sid, lines=lines)
        # Still replying after it, and only OpenCode spoke since (docs/bugs.md, 2026-11-16: after
        # an automatic compaction it continues Qwen itself, and one ran on for three hours with
        # its done.md written): stop the reply and report the file. A lead's or Wai's message
        # since means Qwen is working on that, so wait on.
        if found and still and _qwen_live(hit[0]) and session.auto_continued(q["worktree"], db, found[0][3]):
            word, name, text, _ = found[0]
            ok = stop_pane(q, db, hit[0])  # first: the lead may run check the moment it reads the result
            print(f"{word}: {shown(text)}")
            if ok:
                print(f"mates: stopped Qwen pane {a.n}: it was replying after its {name} only because "
                      f"OpenCode continued it after compacting its context")
            else:
                print(f"mates: Qwen pane {a.n} is still running: OpenCode continued it after compacting "
                      f"its context, past its {name}, and two Escapes did not stop it")
            return 0
        if still and (age := _quiet_s(q["worktree"], db, since)) > STALL_S:
            print("stalled")
            print(f"mates: Qwen pane {a.n} reads busy but OpenCode wrote nothing for {int(age // 60)} minutes; "
                  f"`mates discard {a.n} --share P` and do it yourself", file=sys.stderr)
            return 0
        stopped = 0 if still else stopped + 1
        if stopped >= 2:
            print(f"{found[0][0]}: {shown(found[0][2])}" if found else "idle")
            return 0
        if time.time() >= deadline:
            print("timeout")
            return 1
        time.sleep(min(WAIT_POLL_S, max(0.0, deadline - time.time())))


@_reopening_panels
def answer(args):
    p = _parser("answer", 'mates answer N [--sendback] [--clear-box] "<text>" | mates answer N --update "<text>"')
    p.add_argument("n", type=int)
    p.add_argument("--sendback", action="store_true")
    p.add_argument("--update", default=None)
    p.add_argument("--clear-box", action="store_true")
    p.add_argument("text", nargs="*")
    a = p.parse_args(args)
    if a.update is not None:
        if a.text or a.sendback:
            die("--update takes its own text and is not a sendback: mates answer N --update \"<text>\"")
        text = a.update
    elif not a.text:
        die('mates answer N [--sendback] "<text>"')
    else:
        text = " ".join(a.text)
    root = _lead_root()
    state = _load(root)
    q = _qentry(state, a.n)
    task = q.get("task")
    if not task:
        die(f"Qwen pane {a.n} has no open task (mates send starts one)")
    if a.sendback and task.get("attempts", 0) >= MAX_ATTEMPTS:
        # lead.md: "A second failed attempt ... mates discard N and do the task yourself". The log
        # store got a third one anyway (the retro for 11-18; Wai said yes to refusing it).
        die(f"attempt {task['attempts']} on this task failed: no attempt {task['attempts'] + 1} (lead.md: a second "
            f"failed attempt means discard and do it yourself). mates discard {a.n} --share P (or --fixed "
            f"--share P if you finish Qwen's diff), then do the task yourself")
    pane = _live_pane(root, state, q)
    _stamp_idle(root, a.n)
    db = _db(root)
    if busy(q, db, pane):
        die(f"Qwen pane {a.n} is busy (OpenCode is replying); wait for it")
    _refuse_narrow(a.n, pane)
    _box_guard(a.n, pane, a.clear_box)
    question = _unsafe(read_protocol, q["worktree"], "question.md")
    _unsafe(clear_protocol, q["worktree"])
    now = time.time()
    if a.sendback:
        log_attempt(root, q, "sent-back")

    def apply(e):  # only this task's fields: the same change on the saved entry and on ours
        t = e["task"]
        if a.update is not None:
            t.setdefault("updates", []).append(text)
        elif question is not None or not a.sendback:
            t["questions"].append({"q": question, "a": text})
        if a.sendback:
            t["attempts"] += 1
            t["attempt_ts"] = now
            _clear_review(t)
            e["lead_since"] = now
        t["paste_ts"] = now
    apply(q)
    _change(root, a.n, apply, task_id=task["id"])
    paste(pane, _message(text, q["worktree"]))
    kind = "sendback" if a.sendback else "answer"
    _log_sent(root, a.n, task, kind, text)
    print(f"answered Qwen pane {a.n}" + (f" (sent back: attempt {task['attempts']})" if a.sendback else ""))
    if a.sendback:
        _remind(a.n, task)
    return 0


# --- check, diff ---------------------------------------------------------------------------

def _rmtree(path):
    fsutil.force_rmtree(path)


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
    """N and the command after `--`; no `--`: None (reuse the task's check)."""
    if not args or (len(args) > 1 and (args[1] != "--" or len(args) < 3)):
        die("usage: mates check N [-- <cmd>]")
    rest = args[2:]
    return _int(args[0]), (None if not rest else rest[0] if len(rest) == 1 else shlex.join(rest))


def _sweep_checks(older_than_s=86400):
    """Remove unpacked check copies left by a check that was killed past its cleanup (the
    SIGKILL of a parent); a check lives at most CHECK_TIMEOUT_S, so a day-old folder is stale."""
    base = fsutil.state_dir("checks")
    cutoff = time.time() - older_than_s
    for d in Path(base).iterdir():
        with contextlib.suppress(OSError):
            if d.stat().st_mtime < cutoff:
                _rmtree(d)


def run_check(root, state, n, tree, base, cmd):
    """Run `cmd` in the check sandbox (`qwen exec`) on `tree`, unpacked with `base`'s
    attributes: the one runner for `mates check` and `mates send --check`. Returns
    (exit status, output). The output is Qwen's code's, so control characters are made
    visible (`shown`). Past CHECK_TIMEOUT_S the whole check is killed (its process group;
    the sandbox dies with its parent) and fails with status 124 and a "timed out" line."""
    env = {k: v for k, v in os.environ.items() if k not in SANDBOX_VARS}
    env.update(sandbox_env(root, state.get("runtime")))
    _sweep_checks()
    procs = []

    def killed(sig, _frame):
        for pr in procs:
            with contextlib.suppress(OSError):
                os.killpg(pr.pid, signal.SIGKILL)
            with contextlib.suppress(OSError):
                pr.wait(timeout=5)
        raise SystemExit(128 + sig)  # the `with` below removes the unpacked copy on the way out

    old = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGHUP):
            old[sig] = signal.signal(sig, killed)
    try:
        with _unpacked(root, n, tree, base) as folder:
            env["QWEN_CHECK_DIR"] = str(folder)
            p = subprocess.Popen([session._qwen(), "exec", "--", "sh", "-c", cmd], cwd="/", env=env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 start_new_session=True)
            procs.append(p)
            try:
                raw, _ = p.communicate(timeout=CHECK_TIMEOUT_S)
                code = p.returncode
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                raw, _ = p.communicate()
                raw += f"\nmates: check timed out after {CHECK_TIMEOUT_S} s and was killed\n".encode()
                code = 124
    finally:
        for sig, h in old.items():
            signal.signal(sig, h)
    return code, shown(raw.decode("utf-8", "replace"))


def check(args):
    n, cmd = _check_args(args)
    root = _lead_root()
    state = _load(root)
    q = _qentry(state, n)
    db = _db(root)
    task = q.get("task")
    if cmd is None:
        cmd = (task or {}).get("send_check") or (task or {}).get("check_cmd")
        if task is None or task.get("direct") or not cmd:
            die(f"usage: mates check {n} -- <cmd> (this task has no check to reuse)")
    # Qwen can edit the files mid-check only while qwen runs in its pane: a crashed, failed or
    # closed pane's work stays checkable (a lead checked a pane that died of a segfault).
    hit = _find(root, state, q)
    live = hit is not None and hit[1] != "failed" and \
        _tmux_out("display", "-p", "-t", hit[0], "#{" + QWEN_LIVE + "}").strip() == "1"
    if live and busy(q, db, hit[0]):
        die(f"Qwen pane {n} is busy (OpenCode is replying): Qwen could edit the files mid-check; wait for it")
    task = q.get("task")
    try:
        if task is None:  # Wai's own work with Qwen
            base = git_lead(root, "merge-base", "HEAD", f"refs/heads/{q['branch']}").stdout.strip()
            task = q["task"] = _task(base, direct=True)
        tree = snapshot(root, n, task["base"])
        code, output = run_check(root, state, n, tree, task["base"], cmd)
    except GitError as e:
        die(f"git failed on Qwen worktree {n}: {e}")
    except fsutil.UnsafeDir as e:
        die(f"refusing to check: {e}")
    print(f"check exit {code} on tree {tree[:12]}")
    for line in output.splitlines()[-CHECK_TAIL:]:
        print(line)
    fields = {"checked_tree": tree, "check_cmd": cmd} if code == 0 else {"checked_tree": None}
    task.update(fields)

    def apply(e):  # this task's check fields only; a direct task is attached if still none
        if e.get("task") is None:
            e["task"] = task
        elif e["task"].get("id") == task["id"]:
            e["task"].update(fields)
    _change(root, n, apply)
    return code


# A diff longer than this prints in parts. Claude Code shows a lead only the head of a long tool
# output (a 2 KB preview, and it cuts what it keeps near 30,000 characters), so the lead would
# read less than mates recorded as read. 25,000 leaves room under that cut for the stat, the
# flags and the part line.
DIFF_PART_CHARS = 25000


def _diff_parts(text, limit):
    """`text` cut into parts of at most `limit` characters, at line ends (a longer line is
    cut mid-line). Always at least one part."""
    parts, cur, size = [], [], 0
    for whole in text.splitlines(keepends=True):
        line = whole
        while len(line) > limit:
            if cur:
                parts.append("".join(cur))
                cur, size = [], 0
            parts.append(line[:limit])
            line = line[limit:]
        if size + len(line) > limit and cur:
            parts.append("".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line)
    if cur or not parts:
        parts.append("".join(cur))
    return parts


def _binary_paths(numstat):
    """Paths git counts as binary in `diff --numstat -z` output (`-\t-\t<path>\\0`)."""
    return [rec.split("\t", 2)[2] for rec in numstat.split("\0") if rec.startswith("-\t-\t")]


def _snapshot_diff(root, n, task):
    """(tree, full diff, stat, numstat, guard flags) for Qwen worktree n's snapshot against the
    task's base. Raises GitError."""
    base = task["base"]
    tree = snapshot(root, n, base)
    # --text: a binary-looking file still shows its bytes (attributes come from base anyway)
    full = git_safe(root, n, "diff", "--text", "--no-color", "--no-renames", base, tree, attrs=base).stdout
    stat_ = git_safe(root, n, "diff", "--no-color", "--no-renames", "--stat", base, tree, attrs=base).stdout
    numstat = git_safe(root, n, "diff", "--no-color", "--no-renames", "--numstat", "-z", base, tree,
                       attrs=base).stdout
    hooks = git_lead(root, "config", "core.hooksPath", check=False).stdout.strip() or None
    if hooks and os.path.isabs(hooks) and Path(hooks).resolve().is_relative_to(Path(root)):
        hooks = str(Path(hooks).resolve().relative_to(Path(root)))  # an absolute path inside the repo
    flags = guard.guard_flags(full, task["files"], hooks_path=hooks,
                              runtime_files=runtime.runtime_files(root, base),
                              binary_paths=[gitsafe.lossy(x) for x in _binary_paths(numstat)])
    return tree, full, stat_, numstat, flags


def diff(args):
    p = _parser("diff", "mates diff N [--part K]")
    p.add_argument("n", type=int)
    p.add_argument("--part", type=int, default=1)
    a = p.parse_args(args)
    root = _lead_root()
    state = _load(root)
    q = _qentry(state, a.n)
    task = q.get("task")
    if not task:
        die(f"Qwen pane {a.n} has no open task (mates check N -- <cmd> opens one for Wai's own work)")
    try:
        tree, full, stat_, numstat, flags = _snapshot_diff(root, a.n, task)
    except GitError as e:
        die(f"git failed on Qwen worktree {a.n}: {e}")
    parts = _diff_parts(full, DIFF_PART_CHARS)
    if not 1 <= a.part <= len(parts):
        die(f"this snapshot's diff has {len(parts)} part(s); no part {a.part}")
    seen = task.get("diff_parts") or {}
    if seen.get("tree") != tree or seen.get("total") != len(parts):
        seen = {"tree": tree, "total": len(parts), "seen": []}  # a new snapshot starts the parts over
    seen["seen"] = sorted({*seen["seen"], a.part})
    # The stat and flags come first: a long output may reach the lead cut short, and these are what
    # it must not miss. Qwen wrote these files: escapes made visible, as for check output.
    if a.part == 1:
        print(gitsafe.lossy(stat_).rstrip("\n"))
        print("flags: " + (", ".join(flags) if flags else "none"))
        for path, line in guard.owner_quote_lines(full):  # read each against the brief
            print(shown(gitsafe.lossy(f"{path}: {line[:200]}")))
    if len(parts) > 1:
        print(f"--- diff part {a.part} of {len(parts)} (snapshot {tree[:12]}); the next: mates diff {a.n} --part "
              f"{a.part % len(parts) + 1}; the diff counts as read only after every part")
    sys.stdout.write(shown(gitsafe.lossy(parts[a.part - 1])))
    if task.get("checked_tree") and task["checked_tree"] != tree:
        print(f"mates: this is not the checked snapshot ({task['checked_tree'][:12]}); run mates check {a.n} again",
              file=sys.stderr)
    done = len(seen["seen"]) == len(parts)

    def apply(e):
        e["task"].update(diff_parts=seen, flags=flags)
        if done:
            e["task"].update(diffed_tree=tree)
        elif e["task"].get("diffed_tree") != tree:
            e["task"].update(diffed_tree=None)
    _change(root, a.n, apply, task_id=task["id"])
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
    p = _parser("merge", 'mates merge N [-m "<commit message>"] [--ack kind,...]')
    p.add_argument("n", type=int)
    p.add_argument("-m", dest="summary")
    p.add_argument("--ack", default="", help="flag kinds the lead read and accepts (owner-side, symlink, ...)")
    a = p.parse_args(args)
    root = _lead_root()
    state = _load(root)
    q = _qentry(state, a.n)
    task = q.get("task")
    if not task or not task.get("checked_tree"):
        die(f"nothing checked on Qwen pane {a.n}: run mates check {a.n} -- <cmd> first")
    checked = task["checked_tree"]
    if task.get("send_check") and task.get("check_cmd") != task["send_check"]:
        die(f"the check that passed ({task.get('check_cmd')}) is not the send check "
            f"({task['send_check']}); run mates check {a.n} to re-run it")
    if task.get("diffed_tree") != checked:
        die(f"run mates diff {a.n} first: the lead must read the diff of exactly what merges")
    acked = {k.strip() for k in a.ack.split(",") if k.strip()}
    unacked = [f for f in guard.needs_ack(task.get("flags") or []) if f.split(":", 1)[0] not in acked]
    if unacked:
        kinds = ",".join(sorted({f.split(":", 1)[0] for f in unacked}))
        die("these files run as Wai (or instruct a lead) outside the sandbox once merged: "
            + ", ".join(unacked[:20]) + f". Read each in the diff, then mates merge {a.n} --ack {kinds}")
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
    _stop_reply(root, state, q, a.n)  # the worktree is reset below: a running Qwen would edit the new one
    try:
        message = a.summary or commit_subject(a.n, summary)
        commit = git_lead(root, "commit-tree", checked, "-p", base, "-m", message).stdout.strip()
        git_lead(root, "merge", "--ff-only", "-q", commit, attrs=base)
    except GitError as e:
        die(f"merge failed: {e}", 1)
    task["summary"] = summary  # merged: close the task, then log it, then tidy the worktree
    logged = dict(q)  # still holding the task, for the log line
    q["task"] = None
    now = time.time()  # the 30-minute idle clock restarts when a task ends

    def close(e):
        e["task"] = None
        e["idle_since"] = now
    # Closed before it is logged: a kill in between loses a log line, where the other order left the
    # task open on merged code (a re-merge made an empty duplicate commit and a second accepted line).
    _change(root, a.n, close, task_id=task["id"])
    _clear_brief(root, a.n)
    log_attempt(root, logged, "accepted", qwen_share=100)
    _change(root, a.n, lambda e: e.update(lead_since=logged.get("lead_since")))
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
    _stop_reply(root, state, q, a.n)  # the worktree is reset below: a running Qwen would edit the new one
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
    try:  # Qwen's work is on a ref before anything is overwritten (a kill between the reset and the read-tree)
        kept = _keep(root, a.n, snap)
    except GitError as e:
        die(f"sync failed: {e}", 1)
    try:
        _reset_worktree(root, a.n, q["branch"], lead)
        _, admin, _ = gitsafe.dirs(root, a.n)
        with tempfile.TemporaryDirectory(prefix="mates-sync-index-") as tmp:
            idx = Path(tmp) / "index"
            shutil.copyfile(admin / "index", idx)  # matches the worktree, which is at `lead` now
            git_safe(root, a.n, "read-tree", "-m", "-u", lead, merged, index=idx, attrs=lead)
    except GitError as e:  # the worktree may be half-written now: the ref holds Qwen's work
        die(f"sync failed after the reset ({e}); Qwen's work is kept on {kept}", 1)
    git_lead(root, "update-ref", "-d", kept, check=False)  # it synced: the ref is not needed
    _change(root, a.n, lambda e: (e["task"].update(base=lead), _clear_review(e["task"])), task_id=task["id"])
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


STOP_ARM_S = 3.0  # after the first Escape, how long to wait for "esc again to interrupt"
STOP_S = 20  # after that, how long to wait for OpenCode to stop replying
STOP_POLL_S = 0.25


def _qwen_live(pane):
    return _tmux_out("display", "-p", "-t", pane, "#{" + QWEN_LIVE + "}").strip() == "1"


def stop_pane(q, db, pane):
    """Stop the reply running in `pane` (discard, and wait after OpenCode's own continue);
    True once it reads not busy twice in a row. OpenCode needs two Escapes: the first only
    arms the interrupt (the footer then says "esc again to interrupt"), and a second press
    0.3 s after the first did not take on a real pane. So the second goes in once that footer
    shows, or after STOP_ARM_S, and only while the pane still reads busy (one press is
    enough when it was armed already); then up to STOP_S for the reply to stop."""
    _tmux_out("send-keys", "-t", pane, "Escape")
    second, quiet = time.time() + STOP_ARM_S, 0
    end = second + STOP_S
    while time.time() < end:
        lines = _screen(pane)
        if not busy(q, db, pane, lines=lines):
            quiet += 1
            if quiet >= 2:
                return True
        else:
            quiet = 0
            if second and (ESCAPE_AGAIN.search(_footer(lines, _box_border(lines))) or time.time() >= second):
                _tmux_out("send-keys", "-t", pane, "Escape")
                second = None
        time.sleep(STOP_POLL_S)
    return False


def _stop_reply(root, state, q, n):
    """Stop a busy Qwen before its worktree is reset (docs/bugs.md: one kept editing the reset
    tree for hours). Dies, changing nothing, if it keeps replying."""
    hit = _find(root, state, q)
    if hit is None or hit[1] == "failed":
        return
    pane = hit[0]
    if not _qwen_live(pane):
        return
    db = _db(root)
    if busy(q, db, pane) and not stop_pane(q, db, pane):
        die(f"Qwen pane {n} is still replying after two Escapes; restart it (mates remove {n}, then "
            f"mates add) before discarding")


def discard(args):
    p = _parser("discard", "mates discard N (--share P | --fixed --share P | --failed)")
    p.add_argument("n", type=int)
    p.add_argument("--failed", action="store_true")
    p.add_argument("--fixed", action="store_true",
                   help="you finished Qwen's work and merged it yourself: logged accepted-with-fixes")
    p.add_argument("--share", type=int, metavar="P",
                   help="the share (0-100) of the task Qwen completed, each part weighed by difficulty")
    a = p.parse_args(args)
    if a.failed and a.fixed:
        die("--failed and --fixed say opposite things: pick one")
    if a.fixed and a.share is None:
        die("--fixed needs --share P: the share (0-100) of the task Qwen completed, weighed by difficulty")
    if a.share is not None and not 0 <= a.share <= 100:
        die("--share is a percentage, 0 to 100")
    root = _lead_root()
    state = _load(root)
    q = _qentry(state, a.n)
    task = q.get("task")
    if task and a.share is None and not a.failed:
        # The retro for 11-18: an escalation logged no share, and it was really Qwen's diff the lead
        # finished. Wai said yes to asking for the share on every discard.
        die(f"mates discard {a.n} needs --share P: the share (0-100) of the task Qwen completed, weighed by "
            f"difficulty. If you finish Qwen's diff yourself, that is --fixed --share P (accepted-with-fixes, "
            f"not escalated); --failed if nothing of it is usable")
    _stop_reply(root, state, q, a.n)
    try:
        keep_work(root, a.n, task)
        _reset_worktree(root, a.n, q["branch"], _lead_head(root))
    except GitError as e:
        die(f"discard failed: {e}", 1)
    if task:
        log_attempt(root, q, "failed" if a.failed else "accepted-with-fixes" if a.fixed else "escalated",
                    qwen_share=0 if a.failed else a.share)

    def apply(e):  # this task, the idle clock and the lead-token mark only
        if task and (e.get("task") or {}).get("id") == task["id"]:
            e["task"] = None
        e["idle_since"] = time.time()
        if "lead_since" in q:
            e["lead_since"] = q["lead_since"]
    _change(root, a.n, apply)
    _clear_brief(root, a.n)
    print(f"discarded Qwen pane {a.n}'s task; its worktree is at the lead HEAD")
    return 0


# --- runtime: the lead installs what Qwen's checks need ----------------------------------------
# Wai, 2026-11-11: "allow the leader to install things to that Qwen can run", the sandboxed-install
# option, and "Ensure that Claude doesn't just install whatever Qwen tells it to". The lead commits
# .mates/runtime.sh; this builds it in the build sandbox (runtime.py: only its lockfiles go in,
# only package registries come out), logs why, and restarts the Qwen panes on it.

def _restart_pane(root, pane, q, sid, dest):
    """Restart Qwen pane `pane` on runtime `dest`, in OpenCode session `sid` (its chat kept)."""
    _tmux_out("set-option", "-p", "-u", "-t", pane, QWEN_LIVE)  # -k kills the wrapper before it unsets it
    _tmux_out("respawn-pane", "-k", "-t", pane, "-c", str(root), qwen_cmd(q["worktree"], sid, dest, root))
    session.tag_pane(pane, root, q["n"])  # also clears a failed mark
    release_pane(pane)


def restart_cmd(args):
    """Restart one idle Qwen pane on the current bin/qwen and relays, its chat kept: a pane runs
    the code it started with (2026-11-18: panes started before Wai's own qwen got priority on the
    model kept the old relay)."""
    p = _parser("restart", "mates restart N")
    p.add_argument("n", type=int)
    a = p.parse_args(args)
    root = _lead_root()
    state = _load(root)
    q = next((x for x in state["qwens"] if x["n"] == a.n and not x.get("closed")), None)
    hit = None if q is None else _find(root, state, q)
    if hit is None:
        die(f"no open Qwen pane {a.n}")
    db = _db(root)
    why = _not_restartable(q, hit[0], db)
    if why:
        die(f"Qwen pane {a.n} is {why}; run mates restart {a.n} again when it is idle", 3)
    sid = session.opencode_session(q["worktree"], db)
    _restart_pane(root, hit[0], q, sid, q.get("runtime") or state.get("runtime"))
    print(f"restarted Qwen pane {a.n} ({hit[0]})" + (" with its chat" if sid else ", new chat"))
    return 0


def _log_runtime(root, base, reason, h, outcome, restarted):
    logrec.write(LOG, {"repo": str(root), "task_id": uuid.uuid4().hex[:12], "route": "runtime",
                       "summary": f"runtime {h}: {', '.join(runtime.runtime_files(root, base))}",
                       "reason": reason, "outcome": outcome, "runtime": h, "restarted": restarted})


def _refuse_narrow(n, pane):
    """A pane under layout.MIN_COLS wide or layout.MIN_ROWS tall cannot draw OpenCode, so its
    input box is unreadable and the typing guard would misreport it (human-music, 2026-11-16:
    a 1-column pane)."""
    try:
        width, height = map(int, _tmux_out("display", "-p", "-t", pane, "#{pane_width} #{pane_height}").split())
    except (RuntimeError, ValueError):
        return  # size unknown: the input-box guard still fails closed
    if width < layout.MIN_COLS:
        die(f"Qwen pane {n} is {width} columns wide, too narrow for OpenCode (needs {layout.MIN_COLS}); "
            f"widen it (tmux resize-pane -t {pane} -x 60) or close another pane")
    if height < layout.MIN_ROWS:
        die(f"Qwen pane {n} is {height} rows tall, too short for OpenCode (needs {layout.MIN_ROWS}); "
            f"heighten it (tmux resize-pane -t {pane} -y 20) or close another pane with `mates remove N`")


def _not_restartable(q, pane, db):
    """Why Qwen pane q must not be restarted now, or None."""
    if busy(q, db, pane):
        return "busy (OpenCode is replying)"
    live = _tmux_out("display", "-p", "-t", pane, "#{" + QWEN_LIVE + "}").strip() == "1"
    if live and not input_empty(pane):
        return "holding text in its input box (Wai may be typing)"
    return None


def runtime_cmd(args):
    p = _parser("runtime", 'mates runtime --reason "<what the checks need, and who asked>"')
    p.add_argument("--reason", required=True)
    a = p.parse_args(args)
    root = _lead_root()
    state = _load(root)
    base = git_lead(root, "rev-parse", "HEAD").stdout.strip()  # one commit for every read below
    h = runtime.runtime_hash(root, base)
    if h is None:
        die(f"no {runtime.SCRIPT} committed at HEAD: write it, `git add -f {runtime.SCRIPT}` "
            "(.mates/ is excluded), commit, then run mates runtime again")
    db = _db(root)
    old = state.get("runtime")
    open_ = []
    for q in state["qwens"]:
        hit = None if q.get("closed") else _find(root, state, q)
        if hit is None:
            continue
        why = _not_restartable(q, hit[0], db)
        if why:
            die(f"Qwen pane {q['n']} is {why}; wait, then run mates runtime again")
        q.setdefault("runtime", old)
        open_.append((q, hit[0]))
    print(f"runtime {h} from {base[:12]}: {', '.join(runtime.runtime_files(root, base))}")
    try:
        dest = str(runtime.ensure_runtime(root, base))
    except (RuntimeError, OSError, fsutil.UnsafeDir) as e:
        _log_runtime(root, base, a.reason, h, "failed", [])
        die(f"runtime build failed, panes left as they were: {e}", 1)
    def built(st):  # the top-level runtime, and the old one on entries that had none
        st["runtime"] = dest
        for q, _ in open_:
            e = next((x for x in st["qwens"] if x["n"] == q["n"]), None)
            if e is not None:
                e.setdefault("runtime", old)
    session.update(root, built)
    restarted = []
    for q, pane in open_:
        if q.get("runtime") == dest:
            continue
        why = _not_restartable(q, pane, db)  # again: the build can take minutes
        if why:
            print(f"skipped Qwen pane {q['n']} ({pane}): {why}; run mates runtime again when it is idle")
            continue
        sid = session.opencode_session(q["worktree"], db)
        _restart_pane(root, pane, q, sid, dest)
        q["runtime"] = dest
        _change(root, q["n"], lambda e: e.update(runtime=dest))
        restarted.append(q["n"])
        print(f"restarted Qwen pane {q['n']} ({pane}) on runtime {h}" + (" with its chat" if sid else ", new chat"))
    if not restarted and dest == old and all(q.get("runtime") == dest for q, _ in open_):
        print(f"runtime {h} unchanged; no pane restarted")
        return 0
    _log_runtime(root, base, a.reason, h, "accepted", restarted)
    print(f"runtime {h} ready at {dest}: checks and new panes use it")
    return 0


def parallel_cmd(args):
    p = _parser("parallel", 'mates parallel on|off --reason "<why>"')
    p.add_argument("mode", choices=["on", "off"])
    p.add_argument("--reason", required=True)
    a = p.parse_args(args)
    root = _lead_root()
    _load(root)  # refuses with no state
    session.update(root, lambda st: st.update(parallel=a.mode == "on"))
    logrec.write(LOG, {"repo": str(root), "task_id": uuid.uuid4().hex[:12],
                       "route": "parallel", "summary": f"parallel {a.mode}", "reason": a.reason,
                       "outcome": "accepted"})
    print(f"parallel {a.mode}: {a.reason}")
    return 0


def retro(args):
    import retro as r  # imported here: retro reads cli's constants at import
    return r.main(args)


def _newest_message_time(q, db):
    """Epoch seconds of the newest OpenCode reply in entry q's worktree, 0 with none, or
    None when the database can't be read (the reaper then leaves the pane alone)."""
    try:
        _, _, last_reply = session.opencode_usage(q["worktree"], db, 0)
    except (OSError, sqlite3.Error) as e:
        print(f"mates reap: cannot read Qwen pane {q['n']}'s sessions: {e}", file=sys.stderr)
        return None
    return last_reply or 0


def reap(args):
    """Close Qwen panes unused for IDLE_CLOSE_S seconds."""
    p = _parser("reap", "mates reap [--watch]")
    p.add_argument("--watch", action="store_true")
    a = p.parse_args(args)

    if a.watch:
        root = _lead_root()
        lock = reaper_lock(root)
        lock.parent.mkdir(parents=True, exist_ok=True)
        with open(lock, "a") as f:
            try:  # one watcher per repo: a relaunch finds the first one running and leaves
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            while session.find_lead(root):
                reap([])
                time.sleep(REAP_POLL_S)
        return 0

    root = _lead_root()
    state = _load(root)
    db = _db(root)
    now = time.time()

    def change(n, fn):
        """Change entry n under the state lock, only while it still has no task and is
        open: reap looks for seconds, and a send may attach a task meanwhile. Writing
        reap's own copy of the state back would drop that task."""
        def apply(st):
            e = next((x for x in st.get("qwens", []) if x["n"] == n), None)
            if e is not None and not e.get("task") and not e.get("closed"):
                fn(e)
        session.update(root, apply)

    for q in state["qwens"]:
        if q.get("closed") or q.get("task"):
            continue
        n, hit = q["n"], session.find_pane(root, q["n"])  # not _find: that saves; change() writes the one entry
        if hit is None:
            change(n, lambda e: e.update(closed=True))
            continue
        if q.get("idle_since") is None:  # the first look at this entry starts its clock
            change(n, lambda e: e.update(idle_since=e.get("idle_since") or now))
            continue
        newest = _newest_message_time(q, db)
        if newest is None or now - max(q["idle_since"], newest) < IDLE_CLOSE_S:
            continue
        try:
            if busy(q, db, hit[0]) or not input_empty(hit[0]):
                continue
        except (RuntimeError, OSError, sqlite3.Error) as e:
            print(f"mates reap: left Qwen pane {n} open: {e}", file=sys.stderr)
            continue
        closed = []

        def close(e, pane=hit[0], closed=closed, newest=newest):
            # On the entry as it is now: a send or answer that stamped idle_since while reap looked
            # means the pane is in use (it closed a pane during send's base check, 2026-11-17).
            if time.time() - max(e.get("idle_since") or 0, newest) < IDLE_CLOSE_S:
                return
            r = subprocess.run([layout.TMUX, "kill-pane", "-t", pane], capture_output=True, text=True)
            if r.returncode != 0:
                print(f"mates reap: cannot close {pane}: {r.stderr.strip()}", file=sys.stderr)
                return
            e.update(closed=True, closed_idle=True, idle_since=now)
            closed.append(dict(e))
        change(n, close)
        for e in closed:
            _log_idle_close(root, e)
            print(f"closed idle Qwen pane {n} ({hit[0]})")
    return 0


def _log_idle_close(root, q):
    """Log an idle close as a pane event (route "pane"), never as a Qwen task."""
    logrec.write(LOG, {"repo": str(root), "task_id": uuid.uuid4().hex[:12],
                       "summary": f"idle-close qwen-{q['n']}", "event": "idle-close", "n": q["n"],
                       "route": "pane", "reason": f"unused for {IDLE_CLOSE_S // 60} minutes",
                       "outcome": "closed", "pane": q["n"]})


def reaper_lock(root):
    """The lock that keeps one `mates reap --watch` per repo."""
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "mates" / f"reap-{session.repo_key(str(root))}.lock"


def start_reaper(root):
    """Start `mates reap --watch` for this repo, detached: it ends with the lead pane."""
    env = {**os.environ, "MATES_LEAD": "1", "MATES_ROOT": str(root)}
    subprocess.Popen([sys.executable, str(HERE / "cli.py"), "reap", "--watch"], env=env, cwd=str(root),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


COMMANDS = {"launch": launch, "retro": retro, "send": send, "wait": wait, "answer": answer, "check": check, "diff": diff,
            "merge": merge, "sync": sync, "discard": discard, "add": panes.add, "remove": panes.remove,
            "list": panes.list_, "log": panes.log, "log-route": panes.log_route, "runtime": runtime_cmd, "restart": restart_cmd,
            "parallel": parallel_cmd, "reap": reap, "start": start,
            "followers": followers}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        die("usage: cli.py <command> ...")
    cmd, args = argv[0], argv[1:]
    if cmd not in OPEN_COMMANDS and not _is_lead():
        die(f"`mates {cmd}` runs only from a Claude session (CLAUDECODE=1) or the mates lead (MATES_LEAD=1)")
    if cmd not in COMMANDS:
        die(f"unknown command {cmd!r}")
    try:
        return COMMANDS[cmd](args)
    except GitError as e:  # exit 3: 1 means wait's timeout, which a lead's loop reruns
        die(f"git failed: {e}", 3)
    except RuntimeError as e:  # tmux failures (paste, display, capture-pane)
        die(str(e), 3)
    except sqlite3.Error as e:  # a safety net: Qwen controls OpenCode's database; readers handle it themselves
        die(f"cannot read OpenCode's database: {e}", 3)


if __name__ == "__main__":
    sys.exit(main())
