import decimal
import faulthandler
import json
import time
import uuid

import pytest

from agent_kit.handles import BYTE_CAP, ROW_CAP, HandleError, HandleStore

# fork() from a threaded process warns; this module forks on purpose (see handles.jmes).
pytestmark = pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")


@pytest.fixture(autouse=True)
def hang_guard():
    """If a limit regresses these tests would hang in C code; abort the run instead of stalling."""
    faulthandler.dump_traceback_later(20, exit=True)
    yield
    faulthandler.cancel_dump_traceback_later()


@pytest.fixture
def store():
    s = HandleStore()
    s.put("notes", [{"id": i, "title": f"n{i}", "words": i % 50} for i in range(500)])
    return s


def count(s):
    return s.sql("SELECT count(*) FROM notes")["rows"][0][0]


def test_large_result_becomes_handle_summary(store):
    s = HandleStore().put("big", [{"id": i, "v": "x"} for i in range(500)])
    assert (s.name, s.columns, s.row_count) == ("big", ["id", "v"], 500)
    assert s.sample == [{"id": 0, "v": "x"}, {"id": 1, "v": "x"}, {"id": 2, "v": "x"}]


@pytest.mark.parametrize("stmt", [
    "INSERT INTO notes VALUES (1, 'x', 1)",
    "UPDATE notes SET title = 'pwned'",
    "DELETE FROM notes",
    "DROP TABLE notes",
    "CREATE TABLE evil (a)",
    "ALTER TABLE notes ADD COLUMN c",
    "CREATE TEMP TABLE t AS SELECT * FROM notes",
    "CREATE VIEW v AS SELECT * FROM notes",
    "REPLACE INTO notes VALUES (1, 'x', 1)",
    "WITH x AS (SELECT 1) DELETE FROM notes",
    "VACUUM",
    "REINDEX",
    "ANALYZE",
    "BEGIN",
    "SAVEPOINT s",
])
def test_sql_write_denied(store, stmt):
    with pytest.raises(HandleError):
        store.sql(stmt)
    assert count(store) == 500
    assert store.sql("SELECT title FROM notes WHERE id = 1")["rows"] == [["n1"]]


def test_attach_denied(store, tmp_path):
    target = tmp_path / "stolen.db"
    with pytest.raises(HandleError):
        store.sql(f"ATTACH DATABASE '{target}' AS x")
    assert not target.exists()


@pytest.mark.parametrize("stmt", [
    "PRAGMA table_info(notes)", "PRAGMA writable_schema = 1", "PRAGMA query_only = 0",
])
def test_pragma_denied(store, stmt):
    with pytest.raises(HandleError):
        store.sql(stmt)


@pytest.mark.parametrize("stmt", [
    "SELECT 1; SELECT 2",
    "SELECT 1; DROP TABLE notes",
    "SELECT 1; DELETE FROM notes",
])
def test_multiple_statements_rejected(store, stmt):
    with pytest.raises(HandleError):
        store.sql(stmt)
    assert count(store) == 500


def test_single_statement_with_trailing_semicolon_works(store):
    assert store.sql("SELECT 1;")["rows"] == [[1]]


def test_slow_query_aborted(store):
    import time

    start = time.monotonic()
    with pytest.raises(HandleError, match="timed out"):
        store.sql(
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
            "SELECT count(*) FROM c"
        )
    assert time.monotonic() - start < 3
    assert count(store) == 500  # the connection is still healthy afterwards


def test_giant_blob_refused(store):
    with pytest.raises(HandleError):
        store.sql("SELECT length(zeroblob(900000000))")


def test_row_and_byte_caps(store):
    res = store.sql("SELECT * FROM notes")
    assert len(res["rows"]) == ROW_CAP and res["truncated"] is True
    store.put("wide", [{"id": i, "blob": "x" * 2000} for i in range(100)])
    res = store.sql("SELECT * FROM wide")
    assert res["truncated"] is True
    assert len(json.dumps(res["rows"])) <= BYTE_CAP
    small = store.sql("SELECT id FROM notes LIMIT 3")
    assert len(small["rows"]) == 3 and small["truncated"] is False


def test_jmespath_query(store):
    assert store.jmes("notes", "length(@)")["result"] == 500
    top = store.jmes("notes", "sort_by(@, &words)[-2:].id")["result"]
    assert len(top) == 2
    assert len(store.jmes("notes", "[].id")["result"]) == ROW_CAP  # capped
    with pytest.raises(HandleError):
        store.jmes("notes", "[[[")
    with pytest.raises(HandleError):
        store.jmes("nope", "@")


def test_handles_are_per_task(store):
    other = HandleStore()
    with pytest.raises(HandleError):
        other.sql("SELECT * FROM notes")


def test_nested_values_stored_as_json(store):
    s = HandleStore()
    s.put("t", [{"id": 1, "tags": ["a", "b"]}])
    assert s.sql("SELECT tags FROM t")["rows"] == [['["a", "b"]']]


def test_jmespath_exponential_expression_is_bounded(store):
    """S1: shared-reference doubling and flatten doubling must return fast, not hang or eat RAM."""
    start = time.monotonic()
    share = store.jmes("notes", "@" + " | [@,@]" * 30)  # tiny in memory, exponential to encode
    assert share["truncated"] is True and share["result"] == []
    with pytest.raises(HandleError, match="timed out"):
        store.jmes("notes", "@" + " | [@,@][]" * 40)  # real work doubles every step
    with pytest.raises(HandleError):
        store.jmes("notes", "to_string(@)" + " | [@,@] | to_string(@)" * 40)
    assert time.monotonic() - start < 5
    assert store.jmes("notes", "length(@)")["result"] == 500  # store still works afterwards


def test_jmespath_expression_length_capped(store):
    with pytest.raises(HandleError, match="longer than"):
        store.jmes("notes", "@" + " | @" * 400)


def test_sql_rows_are_not_materialised_past_the_cap(store):
    """S2: a huge result must stop at the cap, not be built first and cut afterwards."""
    evaluated = []
    store._db.create_function("tick", 1, lambda x: evaluated.append(x) or x)
    res = store.sql(
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
        "SELECT tick(x), zeroblob(1000) FROM c"
    )
    assert res["truncated"] is True and len(res["rows"]) <= ROW_CAP
    assert len(evaluated) <= ROW_CAP + 2  # the cursor was abandoned as soon as the cap was hit


def test_put_rejects_case_colliding_columns_and_leaves_no_table():
    s = HandleStore()
    with pytest.raises(HandleError):
        s.put("t", [{"Id": 1, "id": 2}])
    s.put("t", [{"id": 1}])  # the name is free: nothing half-created was left behind


def test_put_wraps_sqlite_errors_and_drops_partial_table():
    s = HandleStore()
    with pytest.raises(HandleError):
        s.put("t", [{"a": 1}, {"a": 2**70}])  # too big for SQLite, fails after row 1 is in
    s.put("t", [{"a": 1}])


def test_put_stringifies_decimal_and_uuid():
    s = HandleStore()
    u = uuid.uuid4()
    s.put("t", [{"price": decimal.Decimal("1.50"), "id": u}])
    assert s.sql("SELECT price, id FROM t")["rows"] == [["1.50", str(u)]]


def test_one_row_cannot_hold_columns_times_a_megabyte(store):
    """N2: per-value and column-count limits bound a single row."""
    with pytest.raises(HandleError):
        store.sql("SELECT length(zeroblob(70000))")  # one cell over the byte cap
    cols = ", ".join(f"zeroblob(60000) AS c{i}" for i in range(150))
    with pytest.raises(HandleError):
        store.sql(f"SELECT {cols}")  # too many columns
    with pytest.raises(HandleError):
        HandleStore().put("wide", [{f"c{i}": 1 for i in range(150)}])


def test_jmespath_memory_limit_is_enforced(store, monkeypatch):
    def hog(expression, data):
        return len(bytearray(2 * 1024**3))

    monkeypatch.setattr("jmespath.search", hog)
    with pytest.raises(HandleError, match="MemoryError"):
        store.jmes("notes", "@")


def test_jmespath_child_death_is_reported(store, monkeypatch):
    import os

    monkeypatch.setattr("jmespath.search", lambda e, d: os._exit(1))
    with pytest.raises(HandleError, match="expression failed"):
        store.jmes("notes", "@")


def test_no_child_processes_left_after_timeout(store):
    import multiprocessing

    with pytest.raises(HandleError, match="timed out"):
        store.jmes("notes", "@" + " | [@,@][]" * 40)
    assert multiprocessing.active_children() == []


def test_timeout_argument_can_only_shorten(store):
    start = time.monotonic()
    with pytest.raises(HandleError, match="timed out"):
        store.jmes("notes", "@" + " | [@,@][]" * 40, timeout_s=0.1)
    assert time.monotonic() - start < 0.5
    with pytest.raises(HandleError, match="timed out"):
        store.sql("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c",
                  timeout_s=60)  # a longer request is clamped to the store's own limit
