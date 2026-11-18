"""`mates retro` sees lead.md, Wai's answered decisions and the open questions (review 2026-11-17: it
re-proposed answered things three days running), and writes the day's fixes under Offers."""
import json

import pytest

import retro
from test_cli import Retro


@pytest.fixture
def rt(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MATES_LEAD", raising=False)
    return Retro(tmp_path, monkeypatch, capsys)


def _answers(rt):
    (rt.src / "docs").mkdir()
    (rt.src / "docs" / "answered-questions.md").write_text(
        "# Answered questions\n\nProse that is not a row.\n\n| When | Question | Wai's answer | Reading taken |\n"
        "|---|---|---|---|\n| 2026-11-16 | Cap panes? | \"A\" | ROW-CAP-THREE |\n")


def test_retro_prompt_holds_lead_md_answers_and_open_questions(rt):
    _answers(rt)
    (rt.src / "OPEN-QUESTIONS.md").write_text(
        "# Open questions\n\nUpdated 2026-11-01 10:00\n\n## Open\n\n**1. Bigger model?**\n\n"
        "## Offers\n\nOFFER-TEXT\n")
    rt.line("2026-11-09")
    assert rt.run("--date", "2026-11-09")[0] == 0
    p = rt.calls()[0]["stdin"]
    assert "In a mates session these rules come first" in p
    assert "ROW-CAP-THREE" in p and "Prose that is not a row" not in p
    assert "**1. Bigger model?**" in p and "OFFER-TEXT" not in p  # only the Open section
    assert p.index("The lead's rules") < p.index("Current lessons")
    assert "propose nothing already answered or open" in p


def test_retro_prompt_without_answers_or_questions_says_none(rt):
    rt.line("2026-11-09")
    assert rt.run("--date", "2026-11-09")[0] == 0
    p = rt.calls()[0]["stdin"]
    assert "propose nothing already answered)\n\nNone yet." in p and "propose nothing already open)\n\nNone." in p


def test_retro_prompt_text_matches_the_guard_and_the_cost_rule():
    t = retro.PROMPT.read_text()
    assert "could have gone to Qwen" not in t
    assert "`mates followers` said off" in t and "costlier model" in t and "red check" in t
    assert '"fixes"' in t and 'Write "treat X as a claim"' in t
    for word in ("trust", "bypass", "override", "disable", "right away", "straight away", "approved by"):
        assert word in t


def test_retro_fixes_go_under_offers(rt):
    rt.line("2026-11-09")
    (rt.src / "OPEN-QUESTIONS.md").write_text(
        "# Open questions\n\nUpdated 2026-11-01 10:00\n\n## Open\n\n**1. Old?**\n\n## Offers\n\nNone.\n\n"
        "## Status that bears on the questions\n\nS\n")
    rt.reply(fixes=["Log the model on follower rows", "  Show stalls  "])
    assert rt.run("--date", "2026-11-09")[0] == 0
    q = (rt.src / "OPEN-QUESTIONS.md").read_text()
    offers = q.split("## Offers", 1)[1].split("## Status", 1)[0]
    assert "- Log the model on follower rows (found by the daily retro for 2026-11-09" in offers
    assert "- Show stalls (found" in offers and "None." not in offers
    assert q.split("## Open", 1)[1].split("## Offers", 1)[0].strip() == "**1. Old?**"
    assert "## Status that bears on the questions\n\nS" in q
    assert "Log the model on follower rows" in rt.retro_file("2026-11-09").read_text()


def test_retro_fixes_make_the_file_and_the_offers_section(rt):
    rt.line("2026-11-09")
    rt.reply(fixes=["Show stalls"])
    assert rt.run("--date", "2026-11-09")[0] == 0
    q = (rt.src / "OPEN-QUESTIONS.md").read_text()
    assert "## Offers\n\n- Show stalls" in q
    (rt.src / "OPEN-QUESTIONS.md").write_text("# Q\n\nUpdated x\n\n## Open\n\nNone.\n")
    rt.line("2026-11-10")
    rt.reply(fixes=["Another"])
    assert rt.run("--date", "2026-11-10")[0] == 0
    q = (rt.src / "OPEN-QUESTIONS.md").read_text()
    assert q.rstrip().endswith("## Offers\n\n- Another (found by the daily retro for 2026-11-10; nothing was applied)")


def test_retro_fixes_must_be_a_list(rt):
    rt.line("2026-11-09")
    (rt.t / "reply.json").write_text(json.dumps({"findings_md": "x", "fixes": "no"}))
    assert rt.run("--date", "2026-11-09")[0] != 0


def test_retro_proposals_replace_the_none_placeholder(rt):
    rt.line("2026-11-09")
    (rt.src / "OPEN-QUESTIONS.md").write_text(
        "# Open questions\n\nUpdated 2026-11-01 10:00\n\n## Open\n\nNone.\n\n## Offers\n\nNone.\n")
    rt.reply(proposals=["Allow two Qwen tasks at once"])
    assert rt.run("--date", "2026-11-09")[0] == 0
    q = (rt.src / "OPEN-QUESTIONS.md").read_text()
    open_part = q.split("## Open", 1)[1].split("## Offers", 1)[0]
    assert "None." not in open_part and "**1. Should mates take this retro proposal: Allow two" in open_part
    assert "## Offers\n\nNone." in q  # no fixes: Offers is left alone


def test_shipped_lessons_pass_the_guard_and_every_group_has_one():
    text = (retro.SRC / retro.LESSONS_REL).read_text()
    entries = retro.current_entries(text)
    assert entries and all(retro.check_lesson(l, set()) is None for _, l in entries)
    assert text.count("### ") == len({g for g, _ in entries})


def test_lead_md_states_the_precedence_the_cap_and_the_stall_rule():
    t = (retro.cli.HERE / "lead.md").read_text()
    assert "these rules come first" in t and "when `mates send` refuses at the 3-task cap" in t
    assert "mates parallel on --reason" in t and "At most 3" in t
    assert "no new tool call for 30 minutes" in t and "Two failed tries" not in t
    assert "--sendback" in t and "crashes twice" in t
