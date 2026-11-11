"""mates: Claude Code (the lead) on the left, sandboxed Qwen panes on the right.

`bin/mates` picks the tmux window and runs `cli.py launch` in the lead's pane:
that starts the Qwen panes, saves the state and prints the lead's `claude` argv
NUL-separated, which `bin/mates` runs as its child. The other subcommands are
the lead's tools and refuse unless MATES_LEAD=1 (except list, log and retro).
"""
import copy
import os
import shlex
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import layout
import runtime
import session

ASK_SUFFIX = ("(If any name, file or expected result above is not stated exactly, write your question "
              "to .mates/question.md and stop before editing. Do not edit tests unless told.)")
HERE = Path(__file__).resolve().parent
LEAD_PROMPT = (HERE / "lead.md", HERE / "lead-lessons.md")
OPEN_COMMANDS = {"launch", "list", "log", "retro"}  # the rest run only from the lead (MATES_LEAD=1)
GIT_SAFE = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")


def die(msg, code=2):
    print(f"mates: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _claude():
    return os.environ.get("MATES_CLAUDE") or "claude"


def _entry(root, n):
    return {"n": n, "worktree": str(Path(root) / ".claude" / "worktrees" / f"mates-qwen-{n}"),
            "branch": f"mates/qwen-{n}", "opencode_session": None, "closed": False, "task": None}


# --- the Qwen pane wrapper ----------------------------------------------------------

def qwen_cmd(worktree, opencode_session=None, runtime_dir=None):
    """The command a Qwen pane runs. It waits for `release_pane` (so the pane is
    tagged before qwen can fail), then runs qwen in the worktree with read-only git.
    Exit 0 closes the pane; any other exit marks it `@mates-failed=1` (the tag
    stays) and leaves a shell with the error."""
    env = [f"QWEN_ROOT={shlex.quote(str(worktree))}", "QWEN_GIT_RO=1"]
    if runtime_dir:
        env.append(f"QWEN_RUNTIME={shlex.quote(str(runtime_dir))}")
    run = [*env, shlex.quote(session._qwen())]
    if opencode_session:
        run += ["-s", shlex.quote(opencode_session)]
    script = ('timeout 30 tmux wait-for "mates-go-$TMUX_PANE"; '
              f'cd {shlex.quote(str(worktree))} && {" ".join(run)}; rc=$?; '
              '[ "$rc" = 0 ] && exit 0; '
              'tmux set-option -p -t "$TMUX_PANE" @mates-failed 1; '
              'echo "mates: qwen exited $rc; this pane is marked failed (mates send refuses it), '
              'a shell instead" >&2; '
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
              "cmd": qwen_cmd(q["worktree"], q["opencode_session"], runtime_dir)}
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


def _admin_dir(root, worktree):
    """The worktree's git admin folder, found from the lead's repo: the entry under
    `<common>/worktrees/` whose `gitdir` file names `<worktree>/.git`."""
    common = Path(subprocess.run(["git", *GIT_SAFE, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                 cwd=root, check=True, capture_output=True, text=True).stdout.strip())
    want = str(Path(worktree) / ".git")
    for d in sorted((common / "worktrees").glob("*")):
        try:
            if (d / "gitdir").read_text().strip() == want:
                return common, d
        except OSError:
            continue
    raise RuntimeError(f"no git worktree entry for {worktree}")


def worktree_dirty(root, n):
    """Paths in Qwen worktree n that differ from its HEAD (untracked included,
    `.mates/` left out); empty means clean. Git never finds the repo from Qwen's
    folder: GIT_DIR and GIT_COMMON_DIR come from the lead's repo, with a temporary
    index and no hooks or fsmonitor. (Task 5's `snapshot`/`git_safe` take this over.)"""
    worktree = Path(root) / ".claude" / "worktrees" / f"mates-qwen-{n}"
    common, admin = _admin_dir(root, worktree)
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_DIR": str(admin), "GIT_COMMON_DIR": str(common),
               "GIT_WORK_TREE": str(worktree), "GIT_INDEX_FILE": str(Path(tmp) / "index")}

        def git(*args):
            return subprocess.run(["git", *GIT_SAFE, "-c", "core.untrackedCache=false", "--no-optional-locks",
                                   *args], cwd=worktree, env=env, check=True, capture_output=True,
                                  text=True).stdout
        git("read-tree", "HEAD")
        out = git("status", "--porcelain", "-z", "-uall")
    paths = [rec[3:] for rec in out.split("\0") if len(rec) > 3]
    return [p for p in paths if not p.startswith(".mates/")]


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
    except (RuntimeError, OSError) as e:
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


COMMANDS = {"launch": launch}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        die("usage: cli.py <command> ...")
    cmd, args = argv[0], argv[1:]
    if cmd not in OPEN_COMMANDS and os.environ.get("MATES_LEAD") != "1":
        die(f"`mates {cmd}` runs only from the mates lead (MATES_LEAD=1)")
    if cmd not in COMMANDS:
        die(f"unknown command {cmd!r}")
    return COMMANDS[cmd](args)


if __name__ == "__main__":
    sys.exit(main())
