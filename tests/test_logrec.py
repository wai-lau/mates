import json, pytest
import logrec

def rec(**kw):
    r = dict(repo="r", task_id="t1", summary="s", route="qwen", reason="checkable", check="bash .mates/check.sh",
             check_sha="abc", red_on_base=True, flags=[], attempts=1, outcome="accepted",
             answered_by=["some-model"], worker_wall_s=10.0, worker_tokens=100, verdict={"pass": True, "findings": 0},
             lead_tokens=50)
    r.update(kw); return r

def test_append_roundtrip(tmp_path):
    p = tmp_path / "log.jsonl"
    logrec.append(p, rec(arm="mates", full_diff_read=False, lead_cache_read=3))
    logrec.append(p, rec(task_id="t2"))
    lines = [json.loads(l) for l in p.read_text().splitlines()]
    assert [l["task_id"] for l in lines] == ["t1", "t2"] and lines[0]["lead_cache_read"] == 3

def test_rejects_unknown_route(tmp_path):
    with pytest.raises(ValueError): logrec.append(tmp_path / "l", rec(route="gpt"))

def test_rejects_unknown_outcome(tmp_path):
    with pytest.raises(ValueError): logrec.append(tmp_path / "l", rec(outcome="maybe"))

def test_rejects_unknown_field(tmp_path):
    with pytest.raises(ValueError): logrec.append(tmp_path / "l", rec(bogus=1))

def test_rejects_missing_required(tmp_path):
    r = rec(); del r["route"]
    with pytest.raises(ValueError): logrec.append(tmp_path / "l", r)

def test_ts_added(tmp_path):
    p = tmp_path / "l"; logrec.append(p, rec())
    assert json.loads(p.read_text())["ts"].endswith("Z")

def test_pane_fields_and_old_pilot_lines_validate(tmp_path):
    p = tmp_path / "l"
    logrec.append(p, rec(pane=2, questions=[{"q": "which?", "a": "add()"}], tree="t" * 40, full_diff_read=True,
                         lead_cache_read=9, red_on_base=None, verdict=None))
    logrec.append(p, rec(arm="mates", full_diff_read=False, lead_cache_read=3))  # a pilot line, as before
    a, b = [json.loads(l) for l in p.read_text().splitlines()]
    assert a["pane"] == 2 and a["questions"][0]["a"] == "add()" and "pane" not in b
