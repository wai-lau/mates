"""Side-by-side tmux panes for mates: Claude on the left, Qwen panes to its right.

A new pane splits off the lead (the first one, taking
half the window) or off the rightmost Qwen pane; `even_qwen_panes` then evens out the
Qwen panes after `mates add` (Wai's call, 2026-11-12). Otherwise Wai resizes by hand, and
`pane_widths` / `restore_widths` carry those widths across a restore.
"""
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


def even_qwen_panes(panes):
    """Even out Qwen pane widths. With k Qwen panes in a W-column window and
    lead L wide, each Qwen pane gets (W - L - k) // k columns. Under 40,
    shrink the lead for 40 each, never below ceil(W/3).
    Returns (resize_commands, warning)."""
    first = next(iter(panes))
    r = _tmux("list-panes", "-t", first, "-F", "#{pane_id} #{pane_left} #{pane_width} #{window_width}")
    if r.returncode != 0:
        raise RuntimeError(f"tmux list-panes failed: {r.stderr.strip()}")

    cols = {}
    lefts = {}
    win_w = 0
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            pid, left, width, win = parts[0], int(parts[1]), int(parts[2]), int(parts[3])
            if pid in panes:
                cols[pid] = width
                lefts[pid] = left
            win_w = win

    lead_pane = None
    lead_cols = 0
    qwen_panes = []
    for p, label in panes.items():
        if label == "lead":
            lead_pane = p
            lead_cols = cols.get(p, 0)
        else:
            qwen_panes.append(p)

    qwen_panes.sort(key=lambda p: lefts.get(p, 0))
    k = len(qwen_panes)
    if k == 0 or win_w == 0:
        return [], None

    target = (win_w - lead_cols - k) // k
    warning = None
    lead_target = lead_cols

    if target < 40:  # the lead gives up just enough for 40 each, never below a third, never grows
        lead_target = min(lead_cols, max(win_w - k * 41, math.ceil(win_w / 3)))
        target = (win_w - lead_target - k) // k

    if target < 40:
        warning = f"Qwen panes are {target} columns wide (under 40); " \
                  f"close one with `mates remove N` to widen them"

    resize = []
    if lead_cols != lead_target:
        resize.append(["resize-pane", "-t", lead_pane, "-x", str(lead_target)])
    for qp in qwen_panes[:-1]:
        resize.append(["resize-pane", "-t", qp, "-x", str(target)])

    return resize, warning
