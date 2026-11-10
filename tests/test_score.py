import math
import pilot

def rec(arm, outcome, tok, tid="t"):
    return {"arm": arm, "outcome": outcome, "lead_tokens": tok, "task_id": tid}

def recs(mates_tok, solo_tok):
    return [rec("mates", "accepted", mates_tok, "a"), rec("solo", "accepted", solo_tok, "a")]

def test_cost_bar_at_50_percent():
    assert pilot.score(recs(50, 100), {}, True, 0)["cost"] is True
    assert pilot.score(recs(51, 100), {}, True, 0)["cost"] is False

def test_cost_counts_failed_attempts_too():
    r = recs(30, 100) + [rec("mates", "escalated", 30, "b")]
    s = pilot.score(r, {}, True, 0)
    assert s["mates_tokens_per_accept"] == 60 and s["cost"] is False

def test_any_false_pass_fails_fidelity():
    assert pilot.score(recs(10, 100), {"mates:a": False}, True, 0)["fidelity"] is False
    assert pilot.score(recs(10, 100), {"mates:a": True}, True, 0)["fidelity"] is True

def test_outbound_nonzero_fails_pii():
    assert pilot.score(recs(10, 100), {}, True, 1)["pii"] is False
    assert pilot.score(recs(10, 100), {}, False, 0)["pii"] is False

def test_no_accepts_fails_cost():
    s = pilot.score([rec("mates", "escalated", 10), rec("solo", "accepted", 100)], {}, True, 0)
    assert math.isinf(s["ratio"]) and s["cost"] is False
