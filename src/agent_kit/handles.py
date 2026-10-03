"""Big tool results become per-task SQLite tables; the model gets a summary and queries it.

Why SQLite + authorizer: the model needs ad-hoc slicing of large data without a code sandbox.
A default-deny authorizer, one statement per call, a time limit and output caps make an
in-memory SQLite connection a small, measurable attack surface.

Create one HandleStore per task: handles are task-scoped and the store is not shared.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterable
from typing import Any

try:  # POSIX only; without it the JMESPath child runs with a deadline but no memory limit
    import resource
except ImportError:  # pragma: no cover
    resource = None  # type: ignore[assignment]

import jmespath
import jmespath.exceptions
from pydantic import BaseModel

_FORK = multiprocessing.get_context("fork")
_CHILD_EXTRA_MEMORY = 512 * 1024 * 1024  # on top of what the forked child already maps
ROW_CAP = 200
MAX_EXPRESSION_CHARS = 500
BYTE_CAP = 64 * 1024
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_QUERY_START = re.compile(r"\s*(?:(?:--[^\n]*\n|/\*.*?\*/)\s*)*(select|with|values)\b", re.I | re.S)
_SQLITE_RECURSIVE = 33  # allow recursive CTEs (they are reads; the timeout bounds them)
_READ_ACTIONS = {
    sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, _SQLITE_RECURSIVE,
}


class HandleError(Exception):
    """A refused or failed handle query. The message is safe to show the model."""


class HandleSummary(BaseModel):
    name: str
    columns: list[str]
    row_count: int
    sample: list[dict[str, Any]]


def _authorizer(action: int, *_: Any) -> int:
    """Default deny: only SELECT/READ/FUNCTION. ATTACH, PRAGMA, REINDEX, writes, DDL all fall through."""
    return sqlite3.SQLITE_OK if action in _READ_ACTIONS else sqlite3.SQLITE_DENY


_ENCODER = json.JSONEncoder(default=str)


def _size(item: Any) -> int:
    """Encoded size, but stops encoding once past the byte cap. A shared-reference structure
    (JMESPath `[@,@]` repeated) is tiny in memory and exponential when fully encoded."""
    n = 0
    for chunk in _ENCODER.iterencode(item):
        n += len(chunk)
        if n > BYTE_CAP:
            break
    return n


def _cap(items: Iterable[Any]) -> tuple[list[Any], bool]:
    """Take items lazily, so a huge SQL cursor is never materialised past the cap."""
    out: list[Any] = []
    size = 0
    for item in items:
        size += _size(item) + 2
        if len(out) >= ROW_CAP or size > BYTE_CAP:
            return out, True
        out.append(item)
    return out, False


def _cell(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    if value is None or isinstance(value, (int, float, str, bytes)):
        return value
    return str(value)  # Decimal, UUID, datetime...


class HandleStore:
    def __init__(self, timeout_s: float = 0.5) -> None:
        self.timeout_s = timeout_s
        self._rows: dict[str, list[dict[str, Any]]] = {}
        # Calls may come from worker threads (so the event loop stays free); the lock serialises them.
        self._lock = threading.Lock()
        self._db = sqlite3.connect(":memory:", check_same_thread=False)
        # Bound memory blow-ups such as zeroblob(1e9) and giant statements.
        self._db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, BYTE_CAP)  # also bounds one row's cells
        self._db.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 100)
        self._db.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 10_000)

    def put(self, name: str, rows: list[dict[str, Any]]) -> HandleSummary:
        with self._lock:
            return self._put(name, rows)

    def _put(self, name: str, rows: list[dict[str, Any]]) -> HandleSummary:
        if not _IDENT.match(name) or name in self._rows:
            raise HandleError(f"bad or duplicate handle name {name!r}")
        if not rows:
            raise HandleError("cannot store an empty result")
        columns = list(dict.fromkeys(k for row in rows for k in row))
        if len({c.lower() for c in columns}) != len(columns):
            raise HandleError("column names collide when case is ignored")
        quote = lambda ident: '"' + ident.replace('"', '""') + '"'  # noqa: E731
        try:
            self._db.execute(f"CREATE TABLE {quote(name)} ({', '.join(map(quote, columns))})")
            self._db.executemany(
                f"INSERT INTO {quote(name)} VALUES ({', '.join('?' * len(columns))})",
                [tuple(_cell(row.get(c)) for c in columns) for row in rows],
            )
        except (sqlite3.Error, sqlite3.Warning, ValueError, OverflowError) as exc:
            self._db.execute(f"DROP TABLE IF EXISTS {quote(name)}")  # no half-created handle
            raise HandleError(f"could not store result: {exc}") from exc
        self._rows[name] = rows
        return HandleSummary(name=name, columns=columns, row_count=len(rows), sample=rows[:3])

    def sql(self, query: str, timeout_s: float | None = None) -> dict[str, Any]:
        with self._lock:
            return self._sql(query, min(timeout_s or self.timeout_s, self.timeout_s))

    def _sql(self, query: str, timeout_s: float) -> dict[str, Any]:
        # The authorizer is the real control. This prefix check also stops statements it never
        # sees, e.g. REINDEX with no indexes to authorize.
        if not _QUERY_START.match(query):
            raise HandleError("query refused: only SELECT queries are allowed")
        deadline = time.monotonic() + timeout_s
        timed_out = False

        def tick() -> int:
            nonlocal timed_out
            timed_out = time.monotonic() > deadline
            return 1 if timed_out else 0

        self._db.set_authorizer(_authorizer)
        self._db.set_progress_handler(tick, 1000)
        try:
            cur = self._db.execute(query)  # sqlite3 itself rejects a second statement
            columns = [d[0] for d in cur.description or []]
            rows, truncated = _cap(list(r) for r in cur)
        except (sqlite3.Error, sqlite3.Warning) as exc:
            if timed_out:
                raise HandleError(f"query timed out after {timeout_s:.2f}s") from exc
            raise HandleError(f"query refused: {exc}") from exc
        finally:
            self._db.set_progress_handler(None, 0)
            self._db.set_authorizer(None)  # type: ignore[arg-type]  # put() needs write access
        return {"columns": columns, "rows": rows, "truncated": truncated}

    def jmes(self, name: str, expression: str, timeout_s: float | None = None) -> dict[str, Any]:
        """JMESPath has no built-in limits (`@ | [@,@][] | [@,@][]...` doubles the work per step),
        so the search runs in a forked child with a deadline and a memory limit, and only the
        already-capped result crosses back."""
        # Simplification: fork needs a POSIX host and is unsafe from heavily threaded hosts; it also costs
        # a few ms per call; upgrade path: a warm worker pool (Windows needs a spawn-based worker).
        if name not in self._rows:
            raise HandleError(f"unknown handle {name!r}")
        if len(expression) > MAX_EXPRESSION_CHARS:
            raise HandleError(f"expression longer than {MAX_EXPRESSION_CHARS} characters")
        timeout_s = min(timeout_s or self.timeout_s, self.timeout_s)
        rows = self._rows[name]
        recv, send = _FORK.Pipe(duplex=False)

        def work() -> None:
            try:
                if resource is not None and os.path.exists("/proc/self/statm"):  # Linux
                    with open("/proc/self/statm") as f:
                        limit = int(f.read().split()[0]) * resource.getpagesize() + _CHILD_EXTRA_MEMORY
                    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
                out = jmespath.search(expression, rows)
                if isinstance(out, list):
                    out, truncated = _cap(out)
                elif _size(out) > BYTE_CAP:
                    raise HandleError("result too large")
                else:
                    truncated = False
                send.send(("ok", {"result": out, "truncated": truncated}))
            except jmespath.exceptions.JMESPathError as exc:
                send.send(("err", f"bad expression: {exc}"))
            except (HandleError, MemoryError, RecursionError) as exc:
                send.send(("err", f"expression refused: {type(exc).__name__} {exc}"))

        proc = _FORK.Process(target=work, daemon=True)
        proc.start()
        send.close()
        try:
            if not recv.poll(timeout_s):
                raise HandleError(f"query timed out after {timeout_s:.2f}s")
            kind, payload = recv.recv()
        except EOFError:  # child died (e.g. killed for memory)
            raise HandleError("expression failed") from None
        finally:
            proc.kill()
            proc.join()
            recv.close()
        if kind == "err":
            raise HandleError(payload)
        return payload
