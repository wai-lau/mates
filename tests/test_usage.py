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


def _data(used, hours_to_reset, five=10):
    reset = datetime.fromtimestamp(NOW + hours_to_reset * 3600, UTC).isoformat()
    return {"usage": {"seven_day": {"utilization": used, "resets_at": reset},
                      "five_hour": {"utilization": five, "resets_at": reset}}}


@pytest.mark.parametrize("used,hours,on,day,left", [
    (71, 23.7, True, 7, 24.0),    # day 7: 95 allowed, 24 left
    (75, 30, False, 6, 6.4),      # day 6: 81.4 allowed, 6.4 left
    (0, 150, False, 1, 13.6),     # day 1: exactly one day's share is not more than one
    (20, 80, True, 4, 34.3),      # day 4: 54.3 allowed
    (90, 4, False, 7, 5.0),       # 4 h before the reset, past the endgame: 5 left of 95
])
def test_headroom(used, hours, on, day, left):
    h = usage.headroom(_data(used, hours), NOW)
    assert (h["on"], h["day"], round(h["left"], 1)) == (on, day, left)


def test_five_hour_window_over_75_turns_followers_off():
    """Wai, 2026-11-16: "above 75% turn off followers"."""
    assert usage.headroom(_data(20, 80, five=75), NOW)["on"] is True
    h = usage.headroom(_data(20, 80, five=76), NOW)
    assert h["on"] is False and "over the 75% limit" in usage.report(h)


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
    p = _write(tmp_path, {"usage": {"seven_day": {"utilization": 50, "resets_at": reset},
                                    "five_hour": {"utilization": 20, "resets_at": reset}}})
    monkeypatch.setattr(usage, "cache_path", lambda: p)
    assert cli.main(["followers"]) == 0  # an open command: Wai can run it too
    out = capsys.readouterr().out
    assert out.startswith("followers on:") and "50%" in out and "13.6%" in out
    p.write_text(json.dumps({"usage": {"seven_day": {"utilization": 90, "resets_at": reset},
                                       "five_hour": {"utilization": 20, "resets_at": reset}}}))
    assert cli.main(["followers"]) == 1
    assert capsys.readouterr().out.startswith("followers off:")


# --- the last 3 hours before the weekly reset (Wai, 2026-11-17: "when there are 3 hours before
# weekly usage refreshes, try to set a pace to reach 99% usage when the usage refreshes, don't
# arbitrarily burn, but significantly downweight model costs during this time"). A pace line rises
# from the cap's 95% at 3 h out to 99% at the reset; followers are on while usage is under it
# (and the 5-hour window allows), and the report says Claude's cost counts low.

@pytest.mark.parametrize("used,hours,on,line", [
    (88, 2.5, True, 95.67),   # 2.5 h left: the line is 95 + 4 * (0.5 / 3)
    (95.5, 1.5, True, 97.0),  # past the old 95% reserve, still under the line
    (97.5, 1.5, False, 97.0), # ahead of the pace: off until the line catches up
    (98.5, 0.1, True, 98.87),     # 6 min left: the line is nearly 99
    (88, 3.5, False, None),   # 3.5 h left: the normal day rule (7% left of 95) applies
])
def test_endgame_pace_line(used, hours, on, line):
    h = usage.headroom(_data(used, hours), NOW)
    assert h["on"] is on
    if line is None:
        assert not h["endgame"]
    else:
        assert h["endgame"] and round(h["line"], 2) == line
        assert ("counts low" in usage.report(h)) is on


def test_endgame_still_minds_the_five_hour_window():
    assert usage.headroom(_data(88, 2, five=80), NOW)["on"] is False


def test_off_lines_say_keep_working():
    """Leads read a bare "off" as "wait for the reset" (2026-11-17): every off line says the
    only change is no new subagents, and an endgame off line drops "counts low"."""
    off = usage.report(usage.headroom(_data(75, 30), NOW))
    assert off.startswith("followers off") and "keep working" in off
    end_off = usage.report(usage.headroom(_data(97.5, 1.5), NOW))
    assert end_off.startswith("followers off") and "keep working" in end_off and "counts low" not in end_off
    stale = usage.report({"on": False, "why": "the usage cache is over 10 minutes old"})
    assert "keep working" in stale


@pytest.mark.parametrize("hours,endgame", [(3.0, True), (3.01, False), (0.0, False), (-0.5, False)])
def test_endgame_window_edges(hours, endgame):
    """Exactly 3 h out is in the endgame; at or past the reset (a stale cache) it is not."""
    assert usage.headroom(_data(50, hours), NOW)["endgame"] is endgame
