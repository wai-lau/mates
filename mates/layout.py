"""Stacked tmux panes for mates: Claude on the left, one column of Qwen panes to its right.

The first Qwen pane splits off the lead (taking half the window); every next one splits the
bottom-most Qwen pane downward, so the Qwen panes stack top to bottom in that one column
(Wai, 2026-11-16: "I prefer splitting the qwen panes horizontally rather than vertically now";
side-by-side columns kept getting too narrow for OpenCode). `even_qwen_panes` evens out their
heights after `mates add` or a launch. `pane_widths` saves the pane order (and each pane's
width) and `restore_widths` puts back the lead's width after a restore.
"""
import itertools
import math
import subprocess

TMUX = "tmux"


class NoRoom(Exception):
    """tmux has no space for another pane; nothing changed."""


def split_qwen(lead_pane, qwen_panes, cwd, cmd):
    """Full tmux argv for the next Qwen pane. `qwen_panes` are top to bottom: the first
    one splits off the lead to the right, the rest split the bottom one downward."""
    if qwen_panes:
        argv = ["tmux", "split-window", "-v", "-P", "-F", "#{pane_id}", "-t", qwen_panes[-1]]
    else:
        argv = ["tmux", "split-window", "-h", "-P", "-F", "#{pane_id}", "-t", lead_pane, "-l", "50%"]
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


# One row per pane: id, left, width, window width, top, height.
_LIST = "#{pane_id} #{pane_left} #{pane_width} #{window_width} #{pane_top} #{pane_height}"


def _geometry(first):
    """{pane id: (left, top, width, height)} for the window of pane `first`, and its width."""
    r = _tmux("list-panes", "-t", first, "-F", _LIST)
    if r.returncode != 0:
        raise RuntimeError(f"tmux list-panes failed: {r.stderr.strip()}")
    geo, win_w = {}, 0
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 6:
            geo[parts[0]] = (int(parts[1]), int(parts[4]), int(parts[2]), int(parts[5]))
            win_w = int(parts[3])
    return geo, win_w


def pane_widths(panes):
    """`panes` maps tmux pane id -> "lead" or Qwen number. Returns
    [{"pane": label, "pct": int}] top to bottom (then left to right), pct of the window
    width. The order is what a restore reopens; only the lead's pct is put back."""
    if not panes:
        return []
    geo, win_w = _geometry(next(iter(panes)))
    rows = [(top, left, panes[pid], round(width * 100 / win_w))
            for pid, (left, top, width, _) in geo.items() if pid in panes]
    return [{"pane": label, "pct": pct} for _, _, label, pct in sorted(rows, key=lambda t: t[:2])]


def restore_widths(lead, widths):
    """After the splits exist, put the lead back to its saved width. Old saved states hold a
    width per pane (side by side); the Qwen ones mean nothing in a stack and are ignored,
    as is anything that is not a sane lead width (then the 50% split stands)."""
    if not widths or widths[0].get("pane") != "lead":
        return
    pct = widths[0].get("pct")
    if isinstance(pct, int) and 0 < pct < 100:
        _tmux("resize-pane", "-t", lead, "-x", f"{pct}%")


MIN_COLS = 40  # narrower, OpenCode cannot draw its input box
# Shorter than this, OpenCode has too little to work in. Its screen is a one-row header, the
# chat, the input box (a border, the text, a model/agent line, a border: about 4 rows) and a
# one-row footer: 6 rows of chrome, so 12 leaves ~6 rows of chat, the least that shows one
# reply with its tool lines. (A 4-pane stack in a 50-row window is 11 each: it warns.)
MIN_ROWS = 12


def even_qwen_panes(panes):
    """Even out Qwen pane heights over the rows the stacked Qwen panes already span, so panes
    mates does not own (a Claude session, a shell) keep theirs. With k stacked Qwen panes
    spanning S rows, each gets (S - (k - 1)) // k. Qwen panes with another pane between them,
    or not in one column, are left alone. If the column is under MIN_COLS wide, a lead
    directly left of it gives up just enough, never going below ceil(W / 3) of the W-column
    window and never growing. Warns about panes under MIN_COLS wide or MIN_ROWS tall.
    Returns (resize_commands, warning)."""
    first = next(iter(panes))
    geo, win_w = _geometry(first)
    lead = next((p for p, label in panes.items() if label == "lead" and p in geo), None)
    qwens = sorted((p for p, label in panes.items() if label != "lead" and p in geo),
                   key=lambda p: geo[p][1])
    k = len(qwens)
    if k == 0 or win_w == 0:
        return [], None
    left = lambda p: geo[p][0]  # noqa: E731
    top = lambda p: geo[p][1]  # noqa: E731
    wid = lambda p: geo[p][2]  # noqa: E731
    hei = lambda p: geo[p][3]  # noqa: E731

    resize = []
    stacked = (len({left(p) for p in qwens}) == 1
               and all(top(b) == top(a) + hei(a) + 1 for a, b in itertools.pairwise(qwens)))
    if stacked:
        width = min(wid(p) for p in qwens)
        if (width < MIN_COLS and lead is not None and left(lead) + wid(lead) + 1 == left(qwens[0])
                and wid(lead) > math.ceil(win_w / 3)):
            lead_target = max(wid(lead) - (MIN_COLS - width), math.ceil(win_w / 3))
            resize.append(["resize-pane", "-t", lead, "-x", str(lead_target)])
            width += wid(lead) - lead_target
        span = top(qwens[-1]) + hei(qwens[-1]) - top(qwens[0])
        target = (span - (k - 1)) // k
        resize += [["resize-pane", "-t", qp, "-y", str(target)] for qp in qwens[:-1]]
        heights = [target] * k if k > 1 else [hei(qwens[0])]
    else:  # resizing would take rows from the pane in between
        width = min(wid(p) for p in qwens)
        heights = [hei(p) for p in qwens]

    problems = []
    if width < MIN_COLS:
        problems.append(f"{width} columns wide (under {MIN_COLS})")
    if min(heights) < MIN_ROWS:
        problems.append(f"{min(heights)} rows tall (under {MIN_ROWS})")
    if not problems:
        return resize, None
    return resize, (f"Qwen panes are {' and '.join(problems)}, too small for OpenCode; "
                    f"widen or heighten the window, close another pane, or `mates remove N`")
