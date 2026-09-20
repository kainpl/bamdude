"""Timing for SQL statements and HTTP requests.

Off by default: with both thresholds at 0 nothing is logged and the top-N table
stays empty, so a farm that has not asked for this pays one ``perf_counter``
pair per statement and nothing else.

Two questions, deliberately separate. *Which statement was slow* is answered by
the WARNING and by :func:`slowest`. *Whether the request was slow because of the
database* is answered by the per-request accumulator, which the HTTP middleware
reads back as ``(db 41 queries, 1620ms)`` — a request that spent 1.6 s of its
1.8 s in the database and one that spent 12 ms there are different bugs, and
without that split they look identical.

⚠️ Never put bound parameters anywhere near a log line. ``bamdude.log`` is what
users attach to public issues and what ``log_health.py`` re-reads; a parameter
is a file path, a spool name or a printer serial. Everything that leaves this
module goes through :func:`fingerprint` first.
"""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field

from sqlalchemy import event

from backend.app.core.logging_filters import redact_url_credentials

logger = logging.getLogger(__name__)

TOP_LIMIT = 200
FINGERPRINT_CHARS = 300

# Key under which the per-connection start-time stack lives in ``conn.info``.
_START_KEY = "bamdude_query_start"

_query_threshold_ms = 0
_request_threshold_ms = 0


def fingerprint(statement: str) -> str:
    """Collapse a statement to one safe, bounded line.

    ⚠️ Redaction happens **before** truncation, and the order is load-bearing:
    ``redact_url_credentials`` anchors on the ``@`` of ``user:secret@host``, so
    slicing first can cut the string short of that anchor and turn the masking
    into a silent no-op on exactly the long lines that most need it. Its own
    docstring says so.
    """
    collapsed = " ".join(statement.split())
    collapsed = redact_url_credentials(collapsed) or ""
    if len(collapsed) > FINGERPRINT_CHARS:
        collapsed = collapsed[:FINGERPRINT_CHARS] + "…"
    return collapsed


def set_query_threshold_ms(ms: int | None) -> None:
    global _query_threshold_ms
    _query_threshold_ms = max(0, int(ms or 0))


def query_threshold_ms() -> int:
    return _query_threshold_ms


def set_request_threshold_ms(ms: int | None) -> None:
    global _request_threshold_ms
    _request_threshold_ms = max(0, int(ms or 0))


def request_threshold_ms() -> int:
    return _request_threshold_ms


@dataclass
class RequestTiming:
    """How much of one request was spent inside the database."""

    count: int = 0
    total_ms: float = 0.0

    def add(self, ms: float) -> None:
        self.count += 1
        self.total_ms += ms


# ⚠️ The value must be a MUTABLE object. Starlette's BaseHTTPMiddleware runs the
# inner app in a child task, and a task COPIES the context at creation — so a
# value set before ``call_next`` is visible inside, but a rebinding made inside
# is not visible outside. Both sides mutating one shared object is what makes
# this work; storing a plain float here would silently always report 0.0.
_timing_var: ContextVar[RequestTiming | None] = ContextVar("bamdude_request_timing", default=None)


def begin_request() -> tuple[RequestTiming, Token]:
    timing = RequestTiming()
    return timing, _timing_var.set(timing)


def end_request(token: Token) -> None:
    _timing_var.reset(token)


def current_timing() -> RequestTiming | None:
    return _timing_var.get()


def log_slow_request(method: str, path: str, elapsed_ms: float, timing: RequestTiming, trace_id: str) -> bool:
    """Log one request that ran over ``slow_request_ms``. Returns whether it did.

    Lives here rather than in the middleware so that «slow query» and «slow
    request» share a logger name and can be filtered and levelled together —
    ``main.py`` forces ``sqlalchemy.engine`` to WARNING outside debug, and ours
    stays ours.

    ⚠️ The path only, never ``request.url.query``: a query string carries filter
    values and, on the camera routes, a stream token.
    """
    threshold = _request_threshold_ms
    if not threshold or elapsed_ms < threshold:
        return False
    logger.warning(
        "slow request %.0fms %s %s (db %d queries, %.0fms) [%s]",
        elapsed_ms,
        method,
        path,
        timing.count,
        timing.total_ms,
        trace_id,
    )
    return True


@dataclass
class SlowStatement:
    statement: str
    count: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    last_seen: float = field(default_factory=time.time)


# Bounded on purpose and never persisted: the numbers describe this process's
# workload, so a restart clearing them is correct. This is also the only source
# of "slowest statements" on SQLite, where pg_stat_statements does not exist.
_top: dict[str, SlowStatement] = {}


def record(fp: str, ms: float) -> None:
    row = _top.get(fp)
    if row is None:
        if len(_top) >= TOP_LIMIT:
            oldest = min(_top.values(), key=lambda r: r.last_seen)
            _top.pop(oldest.statement, None)
        row = SlowStatement(statement=fp)
        _top[fp] = row
    row.count += 1
    row.total_ms += ms
    row.max_ms = max(row.max_ms, ms)
    row.last_seen = time.time()


def slowest(limit: int = 20) -> list[SlowStatement]:
    return sorted(_top.values(), key=lambda r: r.total_ms, reverse=True)[:limit]


def reset() -> None:
    _top.clear()


def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    # A stack, not a scalar: the documented SQLAlchemy recipe, correct under
    # executemany and re-entrancy. One pool connection is held by one greenlet
    # at a time, so it needs no lock.
    conn.info.setdefault(_START_KEY, []).append(time.perf_counter())


def _after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    stack = conn.info.get(_START_KEY)
    if not stack:
        # A statement that began before install() was called.
        return
    elapsed_ms = (time.perf_counter() - stack.pop()) * 1000

    timing = _timing_var.get()
    if timing is not None:
        timing.add(elapsed_ms)

    threshold = _query_threshold_ms
    if not threshold or elapsed_ms < threshold:
        return
    fp = fingerprint(statement)
    record(fp, elapsed_ms)
    logger.warning("slow query %.0fms: %s", elapsed_ms, fp)


def install(engine) -> None:
    """Attach the timing pair to an engine.

    ⚠️ Called from ``_create_engine`` and therefore **outside** its
    ``if is_sqlite()`` branch: the existing ``_strip_tz_from_params`` listener
    lives on the PostgreSQL half only, and hanging the timing there too would
    instrument half the installs. Mixing is safe — SQLAlchemy wraps a non-retval
    ``before_cursor_execute`` listener so it passes ``(statement, parameters)``
    through unchanged.

    Idempotent, because ``reinitialize_database()`` rebuilds the engine after a
    backup restore and may reach here twice for the same object.
    """
    target = engine.sync_engine
    if event.contains(target, "before_cursor_execute", _before_cursor_execute):
        return
    event.listen(target, "before_cursor_execute", _before_cursor_execute)
    event.listen(target, "after_cursor_execute", _after_cursor_execute)
