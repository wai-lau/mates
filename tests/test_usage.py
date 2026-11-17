"""Claude followers only while there is more than one day of Claude usage left under Wai's
pace cap (Wai, 2026-11-16: "enable using Claude followers if there is more than one day
worth of usage left under the pace-snooze"). Same arithmetic as ~/.claude/hooks/pace-cap.py:
the cap allows ceil(days elapsed in the 7-day window) * 95/7 % (never past 95); the headroom
is that minus the week's usage, and followers are on while it is over one day's share."""
import json
import os
import time
from datetime import UTC, datetime

import pytest

import cli
import usage

NOW = 1_800_000_000.0


def _data(used, hours_to_reset):
    reset = datetime.fromtimestamp(NOW + hours_to_reset * 3600, UTC).isoformat()
    return {"usage": {"seven_day": {"utilization": used, "resets_at": reset}}}


@pytest.mark.parametrize("used,hours,on,day,left", [
    (71, 23.7, True, 7, 24.0),    # day 7: 95 allowed, 24 left
    (75, 30, False, 6, 6.4),      # day 6: 81.4 allowed, 6.4 left
    (0, 150, False, 1, 13.6),     # day 1: exactly one day's share is not more than one
    (20, 80, True, 4, 34.3),      # day 4: 54.3 allowed
    (90, 2, False, 7, 5.0),       # past the reserve line soon: 5 left
])
def test_headroom(used, hours, on, day, left):
    h = usage.headroom(_data(used, hours), NOW)
    assert (h["on"], h["day"], round(h["left"], 1)) == (on, day, left)


def _write(tmp_path, data, age_s=0):
    p = tmp_path / "statusline_usage.json"
    p.write_text(json.dumps(data) if not isinstance(data, str) else data)
    os.utime(p, (time.time() - age_s, time.time() - age_s))
    return p


def test_stale_missing_or_broken_usage_means_off(tmp_path):
    assert usage.read(tmp_path / "missing.json")["on"] is False
    assert usage.read(_write(tmp_path, _data(0, 150), age_s=601))["on"] is False
    assert usage.read(_write(tmp_path, "{not json"))["on"] is False
    assert usage.read(_write(tmp_path, {"usage": {}}))["on"] is False


def test_followers_command(tmp_path, monkeypatch, capsys):
    reset = datetime.fromtimestamp(time.time() + 23 * 3600, UTC).isoformat()
    p = _write(tmp_path, {"usage": {"seven_day": {"utilization": 50, "resets_at": reset}}})
    monkeypatch.setattr(usage, "cache_path", lambda: p)
    assert cli.main(["followers"]) == 0  # an open command: Wai can run it too
    out = capsys.readouterr().out
    assert out.startswith("followers on:") and "50%" in out and "13.6%" in out
    p.write_text(json.dumps({"usage": {"seven_day": {"utilization": 90, "resets_at": reset}}}))
    assert cli.main(["followers"]) == 1
    assert capsys.readouterr().out.startswith("followers off:")
