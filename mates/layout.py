"""Side-by-side tmux panes for mates: Claude on the left, Qwen panes to its right.

A new pane splits off the lead (the first one, taking
half the window) or off the rightmost Qwen pane; `even_qwen_panes` then evens out the
Qwen panes after `mates add` over the columns they span (Wai's call, 2026-11-12). Otherwise Wai resizes by hand, and
`pane_widths` / `restore_widths` carry those widths across a restore.
"""
import itertools
import math
import subprocess

TMUX = "tmux"


class NoRoom(Exception):
    """tmux has no space for another pane; nothing changed."""


def split_qwen(lead_pane, qwen_panes, cwd, cmd):
    """Full tmux argv for the next Qwen pane. `qwen_panes` are left to right."""
    argv = ["tmux", "split-window", "-h", "-P", "-F", "#{pane_id}"]
    if qwen_panes:
        argv += ["-t", qwen_panes[-1]]
    else:
        argv += ["-t", lead_pane, "-l", "50%"]
    return argv + ["-c", cwd, cmd]


def _tmux(*args):
    return subprocess.run([TMUX, *args], capture_output=True, text=True)


def split(argv):
    """Run a split argv; return the new pane id. Raises NoRoom if tmux has no space."""
    r = _tmux(*argv[1:])
    if r.returncode != 0:
        if "no space for new pane" in r.stderr:
            raise NoRoom(r.stderr.strip())
        raise RuntimeError(f"tmux split failed: {r.stderr.strip()}")
    return r.stdout.strip()


def pane_widths(panes):
    """`panes` maps tmux pane id -> "lead" or Qwen number. Returns
    [{"pane": label, "pct": int}] left to right, pct of the window width."""
    if not panes:
        return []
    first = next(iter(panes))
    r = _tmux("list-panes", "-t", first, "-F", "#{pane_id} #{pane_left} #{pane_width} #{window_width}")
    if r.returncode != 0:
        raise RuntimeError(f"tmux list-panes failed: {r.stderr.strip()}")
    rows = []
    for line in r.stdout.splitlines():
        pid, left, width, win = line.split()
        if pid in panes:
            rows.append((int(left), panes[pid], round(int(width) * 100 / int(win))))
    return [{"pane": label, "pct": pct} for _, label, pct in sorted(rows, key=lambda t: t[0])]


def restore_widths(panes, widths):
    """After all splits exist (`panes` ids left to right, matching `widths`), put back
    each saved width except the last pane, which takes the rest."""
    for pane, w in list(zip(panes, widths))[:-1]:
        _tmux("resize-pane", "-t", pane, "-x", f"{w['pct']}%")


MIN_COLS = 40  # narrower, OpenCode cannot draw its input box


def even_qwen_panes(panes):
    """Even out Qwen pane widths over the columns the Qwen panes already span, so panes mates
    does not own (a Claude session, a shell) keep theirs. With k side-by-side Qwen panes
    spanning S columns, each gets (S - (k - 1)) // k. Under MIN_COLS, a lead directly left of
    them gives up just enough for MIN_COLS each, never going below ceil(W / 3) of the W-column
    window and never growing. Qwen panes with another pane between them are left alone.
    Returns (resize_commands, warning)."""
    first = next(iter(panes))
    r = _tmux("list-panes", "-t", first, "-F", "#{pane_id} #{pane_left} #{pane_width} #{window_width}")
    if r.returncode != 0:
        raise RuntimeError(f"tmux list-panes failed: {r.stderr.strip()}")

    cols, lefts, win_w = {}, {}, 0
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            pid, left, width, win = parts[0], int(parts[1]), int(parts[2]), int(parts[3])
            if pid in panes:
                cols[pid], lefts[pid] = width, left
            win_w = win

    lead = next((p for p, label in panes.items() if label == "lead" and p in cols), None)
    qwens = sorted((p for p, label in panes.items() if label != "lead" and p in cols), key=lefts.get)
    k = len(qwens)
    if k == 0 or win_w == 0:
        return [], None

    def warn(width):
        return (f"Qwen panes are {width} columns wide (under {MIN_COLS}), too narrow for OpenCode; "
                f"widen the window, close another pane, or `mates remove N`")

    side_by_side = all(lefts[b] == lefts[a] + cols[a] + 1 for a, b in itertools.pairwise(qwens))
    if not side_by_side:  # resizing would take columns from the pane in between
        narrow = min(cols[p] for p in qwens)
        return [], warn(narrow) if narrow < MIN_COLS else None

    span = lefts[qwens[-1]] + cols[qwens[-1]] - lefts[qwens[0]]
    target = (span - (k - 1)) // k
    resize = []
    if target < MIN_COLS and lead is not None and lefts[lead] + cols[lead] + 1 == lefts[qwens[0]]:
        need = k * MIN_COLS + (k - 1) - span  # what the lead must give for MIN_COLS each
        lead_target = min(cols[lead], max(cols[lead] - need, math.ceil(win_w / 3)))
        if lead_target != cols[lead]:
            resize.append(["resize-pane", "-t", lead, "-x", str(lead_target)])
            span += cols[lead] - lead_target
            target = (span - (k - 1)) // k
    for qp in qwens[:-1]:
        resize.append(["resize-pane", "-t", qp, "-x", str(target)])
    return resize, warn(target) if target < MIN_COLS else None
