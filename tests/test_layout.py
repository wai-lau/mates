import shutil
import subprocess

import pytest

import layout


def _stub(tmp_path, body):
    p = tmp_path / "tmux"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return str(p)


def test_first_qwen_splits_lead_half():
    argv = layout.split_qwen("%lead", [], "/r", "qwen")
    assert argv[:2] == ["tmux", "split-window"]
    assert "-h" in argv
    assert argv[argv.index("-t") + 1] == "%lead"
    assert argv[argv.index("-l") + 1] == "50%"


def test_next_qwen_splits_the_bottom_one_downward():
    """Wai, 2026-11-16: "I prefer splitting the qwen panes horizontally rather than
    vertically now": the Qwen panes stack in one column (-v, a divider across)."""
    argv = layout.split_qwen("%lead", ["%2", "%3"], "/r", "qwen")
    assert "-v" in argv and "-h" not in argv
    assert argv[argv.index("-t") + 1] == "%3"
    assert "-l" not in argv


def test_split_argv_has_pane_id_cwd_cmd():
    argv = layout.split_qwen("%lead", [], "/some/repo", "qwen --x")
    assert argv[argv.index("-P") + 0] == "-P"
    assert argv[argv.index("-F") + 1] == "#{pane_id}"
    assert argv[argv.index("-c") + 1] == "/some/repo"
    assert argv[-1] == "qwen --x"


def test_pane_widths_top_to_bottom(tmp_path, monkeypatch):
    """Rows are `id left width window_width top height`; the order is the lead, then the
    Qwen panes from the top down; pct is the pane's width as a share of the window."""
    monkeypatch.setattr(layout, "TMUX", _stub(tmp_path, (
        "echo '%3 120 120 240 40 20'\n"
        "echo '%1 0 120 240 0 60'\n"
        "echo '%2 120 120 240 0 39'\n"
        "echo '%9 999 10 240 0 5'\n")))
    got = layout.pane_widths({"%1": "lead", "%2": 1, "%3": 2})
    assert got == [{"pane": "lead", "pct": 50}, {"pane": 1, "pct": 50}, {"pane": 2, "pct": 50}]


def test_restore_widths_puts_back_only_the_lead(tmp_path, monkeypatch):
    log = tmp_path / "calls"
    monkeypatch.setattr(layout, "TMUX", _stub(tmp_path, f'echo "$@" >> {log}\n'))
    layout.restore_widths("%1", [
        {"pane": "lead", "pct": 40}, {"pane": 1, "pct": 50}, {"pane": 2, "pct": 50}])
    assert log.read_text().splitlines() == ["resize-pane -t %1 -x 40%"]


def test_restore_widths_ignores_odd_saved_shapes(tmp_path, monkeypatch):
    log = tmp_path / "calls"
    monkeypatch.setattr(layout, "TMUX", _stub(tmp_path, f'echo "$@" >> {log}\n'))
    for saved in ([], [{"pane": 1, "pct": 30}], [{"pane": "lead", "pct": 0}],
                  [{"pane": "lead", "pct": 100}], [{"pane": "lead"}], [{"pane": "lead", "pct": "x"}]):
        layout.restore_widths("%1", saved)
    assert not log.exists()


def test_split_no_room_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(layout, "TMUX", _stub(tmp_path, "echo 'no space for new pane' >&2\nexit 1\n"))
    with pytest.raises(layout.NoRoom):
        layout.split(["tmux", "split-window"])


def test_split_returns_pane_id(tmp_path, monkeypatch):
    monkeypatch.setattr(layout, "TMUX", _stub(tmp_path, "echo '%7'\n"))
    assert layout.split(["tmux", "split-window"]) == "%7"


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux absent")
def test_real_tmux_stacked(tmp_path, private_tmux, monkeypatch):
    name = private_tmux
    sock = ["tmux", "-L", name, "-f", "/dev/null"]
    wrap = tmp_path / "tmux-private"
    wrap.write_text(f'#!/bin/sh\nexec tmux -L {name} -f /dev/null "$@"\n')
    wrap.chmod(0o755)
    monkeypatch.setattr(layout, "TMUX", str(wrap))

    def run(*a):
        return subprocess.run([*sock, *a], capture_output=True, text=True, check=True).stdout.strip()

    def geometry():  # pane id -> [left, top, width, height]
        rows = run("list-panes", "-t", name, "-F", "#{pane_id} #{pane_left} #{pane_top} #{pane_width} #{pane_height}")
        return {r.split()[0]: list(map(int, r.split()[1:])) for r in rows.splitlines()}

    try:
        run("new-session", "-d", "-s", name, "-x", "240", "-y", "60", "-c", str(tmp_path), "sleep 60")
        lead = run("display-message", "-p", "-t", name, "#{pane_id}")
        qwens = []
        for _ in range(3):
            argv = layout.split_qwen(lead, qwens, str(tmp_path), "sleep 60")
            argv[0:1] = sock  # same tmux server, private socket
            qwens.append(layout.split(argv))
        info = geometry()
        assert len(info) == 4
        assert info[lead][0] == 0 and info[lead][1] == 0 and info[lead][3] == 60  # the lead keeps the full height
        assert 110 <= info[lead][2] <= 125  # about half the window
        assert len({info[q][0] for q in qwens}) == 1 and info[qwens[0]][0] > info[lead][0]  # one column right of it
        tops = [info[q][1] for q in qwens]
        assert tops == sorted(tops) and len(set(tops)) == 3  # stacked top to bottom

        panes = {lead: "lead", **{q: i + 1 for i, q in enumerate(qwens)}}
        resizes, warning = layout.even_qwen_panes(panes)
        for argv in resizes:
            layout._tmux(*argv)
        heights = [geometry()[q][3] for q in qwens]
        assert max(heights) - min(heights) <= 1 and warning is None
        assert [w["pane"] for w in layout.pane_widths(panes)] == ["lead", 1, 2, 3]
    finally:
        subprocess.run([*sock, "kill-server"], capture_output=True)  # the fixture removes the socket


def test_a_new_qwen_pane_never_takes_the_focus():
    """The tmux tab shows the active pane's title: the lead must stay active."""
    assert "-d" in layout.split_qwen("%0", [], "/w", "qwen")
    assert "-d" in layout.split_qwen("%0", ["%1"], "/w", "qwen")
