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

def test_plan_order_is_seeded_and_alternates():
    ids = [f"t{i}" for i in range(6)]
    p1, p2 = pilot.plan_order(ids, seed=7), pilot.plan_order(ids, seed=7)
    assert p1 == p2 and sorted(t for t, _ in p1) == ids
    assert [arms for _, arms in p1][:2] == [("mates", "solo"), ("solo", "mates")]

def test_outbound_counts_non_loopback(tmp_path):
    log = tmp_path / "net.log"
    log.write_text("ESTAB 0 0 127.0.0.1:5000 127.0.0.1:11434\nESTAB 0 0 10.0.0.2:5000 1.1.1.1:443\n\nESTAB 0 0 [::1]:1 [::1]:2\n")
    assert pilot.count_outbound(log) == 1

def test_report_has_bar_table():
    s = {"pii": True, "fidelity": False, "cost": True, "mates_tokens_per_accept": 100.0,
         "solo_tokens_per_accept": 400.0, "ratio": 0.25}
    md = pilot.render_report(s, {"mates": {"accepted": 3, "escalated": 1, "failed": 0, "wall_s": 100.0},
                                 "solo": {"accepted": 4, "escalated": 0, "failed": 0, "wall_s": 50.0}}, extra="ctx: x")
    assert "| Bar | Pass rule | Measured | Result |" in md and "| Output fidelity |" in md and "FAIL" in md

def test_outbound_ss_headerless_format(tmp_path):
    log = tmp_path / "n"
    log.write_text("0 0 127.0.0.1:45250 127.0.0.1:11434 timer:(x)\n0 0 192.168.1.2:52580 168.144.13.51:22 timer:(y)\n")
    assert pilot.count_outbound(log) == 1
