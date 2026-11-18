"""A database Qwen controls (review 2026-11-17): it is bound read-write into the sandbox, so
mates must never hang or crash on it. Plus the one rule for "is this reply finished"."""
import os
import sqlite3
import time

import pytest

import session
from test_session import _asst, _oc_db, _ses

LOOP = "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r) "


def _hostile(tmp_path, kind):
    p = tmp_path / "opencode.db"
    if kind == "corrupt":
        p.write_bytes(b"this is not a sqlite file at all" * 200)
    elif kind == "unreadable":
        _oc_db(p).close()
        p.chmod(0)
    else:  # a recursive view that never ends, in place of `message`
        db = _oc_db(p)
        db.execute("DROP TABLE message")
        db.execute("CREATE VIEW message AS " + LOOP + "SELECT x AS id, 's' AS session_id, x AS time_created, "
                   "x AS time_updated, '{}' AS data FROM r")
        db.commit()
        db.close()
    return p


@pytest.fixture(params=["view", "corrupt", "unreadable"])
def hostile(tmp_path, request):
    if request.param == "unreadable" and os.geteuid() == 0:
        pytest.skip("root reads anything")
    p = _hostile(tmp_path, request.param)
    yield p
    p.chmod(0o600)


def test_unreadable_database_gives_each_reader_its_cant_read_answer(hostile):
    t0 = time.time()
    now = time.time()
    assert session.opencode_session("/w/1", hostile) is None
    assert session.opencode_state("s", hostile, now - 10) == "busy"    # as with no database: the paste is recent
    assert session.opencode_state("s", hostile, now - 1000) == "idle"  # a crashed OpenCode stops counting
    assert session.opencode_mid_reply("s", hostile) is None
    assert session.auto_continued("/w/1", hostile, 0) is False
    with pytest.raises(sqlite3.Error):
        session.opencode_usage("/w/1", hostile, 0)
    with pytest.raises(sqlite3.Error):
        session.opencode_chats("/r", hostile, "2026-11-09")
    assert time.time() - t0 < 3


def test_a_slow_query_is_aborted_by_the_budget(tmp_path, monkeypatch):
    p = tmp_path / "opencode.db"
    _oc_db(p).close()
    monkeypatch.setattr(session, "DB_BUDGET_S", 0.3)
    t0 = time.time()
    with pytest.raises(sqlite3.Error), session._ro(p) as con:
        con.execute(LOOP + "SELECT max(x) FROM r").fetchone()
    assert time.time() - t0 < 3


def test_the_connection_cannot_write(tmp_path):
    p = tmp_path / "opencode.db"
    _oc_db(p).close()
    with pytest.raises(sqlite3.Error), session._ro(p) as con:
        con.execute("CREATE TABLE x (a)")


@pytest.mark.parametrize("name", ["session", "message", "part"])
def test_a_view_or_missing_table_is_a_bad_schema(tmp_path, name):
    p = tmp_path / "opencode.db"
    db = _oc_db(p)
    db.execute(f"DROP TABLE {name}")
    db.execute(f"CREATE VIEW {name} AS SELECT 1 AS id")
    db.commit()
    db.close()
    with pytest.raises(sqlite3.Error), session._ro(p):
        pass


def test_a_message_that_is_not_an_object_is_skipped_not_a_crash(tmp_path):
    now = time.time()
    p = tmp_path / "opencode.db"
    db = _oc_db(p)
    db.execute("INSERT INTO message VALUES ('m1', 's', ?, ?, '\"just a string\"')", (int(now * 1000),) * 2)
    db.commit()
    assert session.opencode_state("s", p, now - 10) == "busy"
    assert session.opencode_mid_reply("s", p) is None
    assert session.opencode_usage("/w/1", p, 0) == (0, 0, None)


@pytest.mark.parametrize(("data", "finished", "compaction"), [
    ({"role": "assistant", "time": {"completed": 5}, "finish": "stop"}, True, False),
    ({"role": "assistant", "time": {"completed": 5}}, True, False),
    ({"role": "assistant", "time": {}}, False, False),
    ({"role": "assistant", "time": {"completed": 5}, "finish": "tool-calls"}, False, False),
    ({"role": "assistant", "time": {"completed": 5}, "finish": "tool-calls", "error": {"name": "X"}}, True, False),
    ({"role": "assistant", "time": {"completed": 5}, "summary": True}, True, True),
    ({"role": "assistant", "time": {"completed": 5}, "summary": "yes"}, True, True),
    ({"role": "assistant", "time": {"completed": 5}, "mode": "compaction"}, True, True),
    ({"role": "assistant", "time": {"completed": 5}, "agent": "compaction"}, True, True),
    ({"role": "user"}, False, False),
])
def test_reply_classifier(data, finished, compaction):
    assert session.reply_finished(data) is finished
    assert session.reply_compaction(data) is compaction


def test_an_escape_abort_ends_the_reply_in_state_and_mid_reply(tmp_path):
    """A tool-calls step that stopped on an error is finished for every reader."""
    now = time.time()
    p = tmp_path / "opencode.db"
    db = _oc_db(p)
    _ses(db, "s", "/w/1", 1000)
    _asst(db, "m1", now - 20, finish="tool-calls", error={"name": "MessageAbortedError"},
          time={"created": int((now - 20) * 1000), "completed": int((now - 15) * 1000)})
    assert session.opencode_state("s", p, now - 30) == "idle"
    assert session.opencode_mid_reply("s", p) is None


def test_usage_counts_any_truthy_summary_as_a_compaction(tmp_path):
    now = time.time()
    p = tmp_path / "opencode.db"
    db = _oc_db(p)
    _ses(db, "s", "/w/1", 1000)
    db.execute("INSERT INTO session VALUES ('s2', 'p', NULL, '/w/1', 't', 1, 1)")
    db.commit()
    data = '{"role": "assistant", "summary": "yes", "time": {"created": 1, "completed": 2}}'
    db.execute("INSERT INTO message VALUES ('m1', 's', ?, ?, ?)", (int(now * 1000), int(now * 1000), data))
    db.commit()
    assert session.opencode_usage("/w/1", p, 0)[1] == 1
