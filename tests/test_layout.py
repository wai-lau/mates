import os
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


def test_next_qwen_splits_rightmost():
    argv = layout.split_qwen("%lead", ["%2", "%3"], "/r", "qwen")
    assert "-h" in argv
    assert argv[argv.index("-t") + 1] == "%3"
    assert "-l" not in argv


def test_split_argv_has_pane_id_cwd_cmd():
    argv = layout.split_qwen("%lead", [], "/some/repo", "qwen --x")
    assert argv[argv.index("-P") + 0] == "-P"
    assert argv[argv.index("-F") + 1] == "#{pane_id}"
    assert argv[argv.index("-c") + 1] == "/some/repo"
    assert argv[-1] == "qwen --x"


def test_pane_widths_left_to_right(tmp_path, monkeypatch):
    monkeypatch.setattr(layout, "TMUX", _stub(tmp_path, (
        "echo '%3 120 60 240'\n"
        "echo '%1 0 50 240'\n"
        "echo '%2 60 60 240'\n"
        "echo '%9 999 10 240'\n")))
    got = layout.pane_widths({"%1": "lead", "%2": 1, "%3": 2})
    assert got == [{"pane": "lead", "pct": 21}, {"pane": 1, "pct": 25}, {"pane": 2, "pct": 25}]


def test_restore_widths_reapplied(tmp_path, monkeypatch):
    log = tmp_path / "calls"
    monkeypatch.setattr(layout, "TMUX", _stub(tmp_path, f'echo "$@" >> {log}\n'))
    layout.restore_widths(["%1", "%2", "%3"], [
        {"pane": "lead", "pct": 50}, {"pane": 1, "pct": 20}, {"pane": 2, "pct": 30}])
    assert log.read_text().splitlines() == [
        "resize-pane -t %1 -x 50%", "resize-pane -t %2 -x 20%"]


def test_split_no_room_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(layout, "TMUX", _stub(tmp_path, "echo 'no space for new pane' >&2\nexit 1\n"))
    with pytest.raises(layout.NoRoom):
        layout.split(["tmux", "split-window"])


def test_split_returns_pane_id(tmp_path, monkeypatch):
    monkeypatch.setattr(layout, "TMUX", _stub(tmp_path, "echo '%7'\n"))
    assert layout.split(["tmux", "split-window"]) == "%7"


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux absent")
def test_real_tmux_side_by_side(tmp_path, private_tmux):
    name = private_tmux
    sock = ["tmux", "-L", name, "-f", "/dev/null"]

    def run(*a):
        return subprocess.run([*sock, *a], capture_output=True, text=True, check=True).stdout.strip()

    try:
        run("new-session", "-d", "-s", name, "-x", "240", "-y", "60", "-c", str(tmp_path), "sleep 60")
        lead = run("display-message", "-p", "-t", name, "#{pane_id}")
        qwens = []
        for _ in range(2):
            argv = layout.split_qwen(lead, qwens, str(tmp_path), "sleep 60")
            argv[0:1] = sock  # same tmux server, private socket
            qwens.append(layout.split(argv))
        rows = run("list-panes", "-t", name, "-F", "#{pane_id} #{pane_top} #{pane_left} #{pane_width}").splitlines()
        info = {r.split()[0]: list(map(int, r.split()[1:])) for r in rows}
        assert len(info) == 3
        assert {v[0] for v in info.values()} == {0}
        assert [p for p, _ in sorted(info.items(), key=lambda kv: kv[1][1])] == [lead, *qwens]
        assert 110 <= info[lead][2] <= 125
    finally:
        subprocess.run([*sock, "kill-server"], capture_output=True)  # the fixture removes the socket
