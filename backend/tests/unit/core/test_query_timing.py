"""The timing module on its own — no engine, no app.

Thresholds are module state applied at runtime, the top-N table is bounded and
evicts the least recently seen, and a fingerprint never carries a value.
"""

import inspect

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core import query_timing as qt


@pytest.fixture(autouse=True)
def _clean():
    qt.reset()
    qt.set_query_threshold_ms(0)
    qt.set_request_threshold_ms(0)
    yield
    qt.reset()
    qt.set_query_threshold_ms(0)
    qt.set_request_threshold_ms(0)


def test_a_fingerprint_collapses_whitespace_and_truncates():
    assert qt.fingerprint("SELECT   a,\n   b\nFROM t") == "SELECT a, b FROM t"
    long = qt.fingerprint("SELECT " + "x" * 1000)
    assert len(long) == qt.FINGERPRINT_CHARS + 1  # the ellipsis
    assert long.endswith("…")


def test_a_fingerprint_redacts_credentials_in_a_url():
    fp = qt.fingerprint("SELECT 'postgresql://bamdude:hunter2@host/db'")
    assert "hunter2" not in fp
    assert "bamdude" in fp


def test_redaction_happens_before_truncation():
    """⚠️ The order is the whole point: a slice can remove the ``@`` the pattern
    anchors on, and then the masking silently does nothing. Put the credentials
    past the truncation boundary and the password must still be gone."""
    statement = "SELECT " + "x" * qt.FINGERPRINT_CHARS + " 'postgresql://u:hunter2@host/db'"
    assert "hunter2" not in qt.fingerprint(statement)


def test_thresholds_default_to_off_and_are_settable():
    assert qt.query_threshold_ms() == 0
    assert qt.request_threshold_ms() == 0
    qt.set_query_threshold_ms(250)
    qt.set_request_threshold_ms(3000)
    assert qt.query_threshold_ms() == 250
    assert qt.request_threshold_ms() == 3000
    # A None or a negative from the settings row is off, never a crash.
    qt.set_query_threshold_ms(None)
    qt.set_request_threshold_ms(-5)
    assert qt.query_threshold_ms() == 0
    assert qt.request_threshold_ms() == 0


def test_the_top_table_accumulates_per_fingerprint():
    qt.record("SELECT 1", 10.0)
    qt.record("SELECT 1", 30.0)
    qt.record("SELECT 2", 5.0)
    rows = {r.statement: r for r in qt.slowest()}
    assert rows["SELECT 1"].count == 2
    assert rows["SELECT 1"].total_ms == pytest.approx(40.0)
    assert rows["SELECT 1"].max_ms == pytest.approx(30.0)
    assert [r.statement for r in qt.slowest()] == ["SELECT 1", "SELECT 2"]


def test_the_top_table_is_bounded_and_drops_the_least_recently_seen():
    for i in range(qt.TOP_LIMIT + 10):
        qt.record(f"SELECT {i}", 1.0)
    assert len(qt.slowest(limit=10_000)) == qt.TOP_LIMIT
    kept = {r.statement for r in qt.slowest(limit=10_000)}
    assert "SELECT 0" not in kept
    assert f"SELECT {qt.TOP_LIMIT + 9}" in kept


def test_a_request_accumulator_is_visible_through_the_context_var():
    assert qt.current_timing() is None
    timing, token = qt.begin_request()
    try:
        qt.current_timing().add(12.5)
        qt.current_timing().add(2.5)
        assert timing.count == 2
        assert timing.total_ms == pytest.approx(15.0)
    finally:
        qt.end_request(token)
    assert qt.current_timing() is None


@pytest.mark.asyncio
async def test_the_listeners_time_a_real_statement_once(tmp_path):
    """⚠️ conftest's test_engine attaches none of database.py's listeners, so
    this installs them on an engine of its own — that is the contract, not a
    workaround.

    ``install`` is called twice on purpose: a backup restore rebuilds the engine
    and can reach it again, and a double attach would count every statement
    twice. One statement must therefore produce exactly one ``add``.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'probe.db'}")
    qt.install(engine)
    qt.install(engine)

    timing, token = qt.begin_request()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        qt.end_request(token)
        await engine.dispose()

    assert timing.count == 1
    assert timing.total_ms > 0


def test_a_statement_over_the_threshold_is_recorded_and_logged(caplog):
    """Drives the listener directly, because how long SQLite takes to answer
    ``SELECT 1`` is not something a test may assert on."""

    class _Conn:
        info: dict = {}

    conn = _Conn()
    conn.info = {}
    qt.set_query_threshold_ms(100)
    qt._before_cursor_execute(conn, None, "SELECT 1", None, None, False)
    conn.info[qt._START_KEY][0] -= 0.5  # pretend it started half a second ago

    with caplog.at_level("WARNING"):
        qt._after_cursor_execute(conn, None, "SELECT   1", None, None, False)

    assert [r.statement for r in qt.slowest()] == ["SELECT 1"]
    assert qt.slowest()[0].max_ms >= 500
    assert "slow query" in caplog.text


def test_a_statement_under_the_threshold_is_not_recorded():
    class _Conn:
        info: dict = {}

    conn = _Conn()
    conn.info = {}
    qt.set_query_threshold_ms(100)
    qt._before_cursor_execute(conn, None, "SELECT 1", None, None, False)
    qt._after_cursor_execute(conn, None, "SELECT 1", None, None, False)
    assert qt.slowest() == []


@pytest.mark.asyncio
async def test_nothing_is_recorded_while_the_threshold_is_zero(tmp_path):
    """Off must mean off: the accumulator still fills, the log and the table do
    not."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'probe.db'}")
    qt.install(engine)

    timing, token = qt.begin_request()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        qt.end_request(token)
        await engine.dispose()

    assert timing.count >= 1
    assert qt.slowest() == []


def test_the_pair_is_attached_for_both_dialects():
    """The tz-strip listener is PostgreSQL-only; ours must not inherit that."""
    source = inspect.getsource(__import__("backend.app.core.database", fromlist=["x"])._create_engine)
    lines = source.splitlines()
    install_line = next(line for line in lines if "query_timing.install" in line)
    branch_indent = min(len(line) - len(line.lstrip()) for line in lines if "if is_sqlite()" in line)
    assert len(install_line) - len(install_line.lstrip()) <= branch_indent
