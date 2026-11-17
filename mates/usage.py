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
STALE_S = 600  # the statusline refreshes the cache every 60 s; the cap ignores it past 10 min


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
    return {"on": left > DAY_SHARE and five <= FIVE_HOUR_MAX, "used": used, "day": day, "allowed": allowed,
            "left": left, "five_hour": five}


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


def report(h):
    """One line for the lead."""
    if "why" in h:
        return f"followers off: {h['why']}"
    state = "on" if h["on"] else "off"
    than = "more" if h["left"] > DAY_SHARE else "not more"
    five = "under" if h["five_hour"] <= FIVE_HOUR_MAX else "over"
    return (f"followers {state}: 7-day usage {h['used']:.0f}%, the pace cap allows {h['allowed']:.1f}% "
            f"(day {h['day']} of 7), so {h['left']:.1f}% is left, {than} than one day's share "
            f"({DAY_SHARE:.1f}%); 5-hour usage {h['five_hour']:.0f}%, {five} the {FIVE_HOUR_MAX:.0f}% limit")
