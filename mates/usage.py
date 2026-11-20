"""Claude usage left under Wai's pace cap, for Claude followers.

Wai, 2026-11-16: "remember the thing about using stronger agents for complex problems or
increasing throughput? / enable using Claude followers if there is more than one day worth
of usage left under the pace-snooze". The pace cap (~/.claude/hooks/pace-cap.py, snoozed
with `pace-snooze`) allows ceil(days elapsed in the 7-day window) * 95/7 % of the week's
usage, never past 95 %. The headroom is that minus what the week has used; followers are on
while it is more than one day's share (95/7 %), and the rolling 5-hour window is at most
FIVE_HOUR_MAX used (Wai, 2026-11-16: "above 75% turn off followers": a burst of followers
fills that window, and then every session stalls). Usage comes from the statusline's cache,
as for the cap; missing, stale or unreadable usage means off (never spend blind).
"""
import json
import math
import os
import time
from datetime import datetime
from pathlib import Path

RESERVE = 95.0  # the cap's reserve line: the last 5 % is spent only by a snooze
WEEK = 7 * 86400
DAY_SHARE = RESERVE / 7
FIVE_HOUR_MAX = 75.0
# The last ENDGAME_S before the weekly reset (Wai, 2026-11-17: "when there are 3 hours before
# weekly usage refreshes, try to set a pace to reach 99% usage when the usage refreshes, don't
# arbitrarily burn, but significantly downweight model costs during this time"): a pace line
# rises from RESERVE at the window's start to ENDGAME_TARGET at the reset, and followers are on
# while usage is under it. ~/.claude/hooks/pace-cap.py lets prompts through under the same line.
ENDGAME_S = 3 * 3600
ENDGAME_TARGET = 99.0
STALE_S = 600  # the statusline refreshes the cache every 60 s; the cap ignores it past 10 min


def snooze_path():
    return Path.home() / ".claude" / "cache" / "pace-snooze"


def _snoozed_until(now):
    """When Wai's pace-snooze ends (epoch s), or None when it is not on. A snooze means work
    through the paced limits (Wai, 2026-11-18: "for pace-snooze, if I've toggled it on, it means
    work through paced limits / no Claude followers still applies though")."""
    try:
        until = float(snooze_path().read_text().strip())
    except (OSError, ValueError):
        return None
    return until if until > now else None


def cache_path():
    return Path.home() / ".claude" / "cache" / "statusline_usage.json"


def headroom(data, now):
    """{"on", "used", "day", "allowed", "left", "five_hour"} from the statusline's usage data."""
    sd = data["usage"]["seven_day"]
    five = float(data["usage"]["five_hour"]["utilization"])
    used = float(sd["utilization"])
    reset = datetime.fromisoformat(sd["resets_at"]).timestamp()
    day = max(1, math.ceil(max(0.0, now - (reset - WEEK)) / 86400))
    allowed = min(day * RESERVE / 7, RESERVE)
    left = allowed - used
    to_reset = reset - now
    h = {"used": used, "day": day, "allowed": allowed, "left": left, "five_hour": five,
         "endgame": 0 < to_reset <= ENDGAME_S, "to_reset": to_reset, "snoozed_until": _snoozed_until(now)}
    if h["endgame"]:
        h["line"] = endgame_line(to_reset)
        h["on"] = used < h["line"] and five <= FIVE_HOUR_MAX
    else:
        h["on"] = left > DAY_SHARE and five <= FIVE_HOUR_MAX
    return h


def endgame_line(to_reset):
    """The pace line `to_reset` seconds before the weekly reset, in the last ENDGAME_S."""
    return RESERVE + (ENDGAME_TARGET - RESERVE) * (1 - to_reset / ENDGAME_S)


def read(path=None, now=None):
    """headroom() of the cache, or {"on": False, "why": ...} when it cannot be trusted."""
    path = Path(path or cache_path())
    now = time.time() if now is None else now
    try:
        if now - os.stat(path).st_mtime > STALE_S:
            return {"on": False, "why": f"the usage cache is over {STALE_S // 60} minutes old"}
        return headroom(json.loads(path.read_text()), now)
    except (OSError, ValueError, KeyError, TypeError) as e:
        return {"on": False, "why": f"no usable usage data ({type(e).__name__})"}


KEEP_WORKING = ("No new Claude subagents; keep working now: Qwen is your follower, so hand it what you would "
                "hand a Claude follower when a red check proves it done, and do the rest yourself")
# Qwen is never paced (Wai, 2026-11-20: "qwen is always allowed as a follower! qwen needs no pace"):
# a bare "followers off" read as Qwen off too, and both leads sent Qwen nothing for a day.
QWEN = "Qwen followers: always on, no pace. "


def report(h):
    """One line for the lead (plus a note when Wai has snoozed the pace cap)."""
    line = _report(h)
    if h.get("snoozed_until"):
        # First, so the allowance figures after it never read as "wait" (Wai, 2026-11-18, to a lead
        # that scheduled a later pickup while snoozed: "NO / pace-snooze is on, means keep working,
        # just don't launch Claude followers").
        line = (f"pace cap snoozed until {time.strftime('%H:%M', time.localtime(h['snoozed_until']))}: keep "
                "working now through the paced limits, with Qwen and yourself; never wait for the allowance. "
                "Claude followers still follow this switch: " + line)
    return QWEN + line


def _report(h):
    if "why" in h:
        return f"Claude followers off: {h['why']}. {KEEP_WORKING}"
    state = "on" if h["on"] else "off"
    if h.get("endgame"):
        five = "under" if h["five_hour"] <= FIVE_HOUR_MAX else "over"
        where = "under" if h["used"] < h["line"] else "at or over"
        return (f"Claude followers {state}: endgame, {h['to_reset'] / 3600:.1f} h to the weekly reset. 7-day usage "
                f"{h['used']:.0f}% is {where} the pace line {h['line']:.1f}% (rising to {ENDGAME_TARGET:.0f}% at "
                f"the reset); 5-hour usage {h['five_hour']:.0f}%, {five} the {FIVE_HOUR_MAX:.0f}% limit. "
                + ("Until the reset Claude's cost counts low: real work only, no make-work" if h["on"]
                   else KEEP_WORKING))
    than = "more" if h["left"] > DAY_SHARE else "not more"
    five = "under" if h["five_hour"] <= FIVE_HOUR_MAX else "over"
    return (f"Claude followers {state}: 7-day usage {h['used']:.0f}%, the pace cap allows {h['allowed']:.1f}% "
            f"(day {h['day']} of 7), so {h['left']:.1f}% is left, {than} than one day's share "
            f"({DAY_SHARE:.1f}%); 5-hour usage {h['five_hour']:.0f}%, {five} the {FIVE_HOUR_MAX:.0f}% limit"
            + ("" if h["on"] else f". {KEEP_WORKING}"))
