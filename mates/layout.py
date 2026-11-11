"""Side-by-side tmux panes for mates: Claude on the left, Qwen panes to its right.

Nothing here re-tiles. A new pane splits off the lead (the first one, taking
half the window) or off the rightmost Qwen pane; Wai resizes by hand, and
`pane_widths` / `restore_widths` carry those widths across a restore.
"""
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
