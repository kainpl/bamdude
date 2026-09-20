"""Database health, with the failure path tested first.

The panel exists for the moment the database is unhappy, so «one probe raised
and the other nine still answered» is the case that matters most.
"""

import pytest

from backend.app.core import query_timing as qt
from backend.app.services import db_health


@pytest.fixture(autouse=True)
def _clean_instrumentation():
    qt.reset()
    qt.set_query_threshold_ms(0)
    yield
    qt.reset()
    qt.set_query_threshold_ms(0)


@pytest.mark.parametrize(
    ("embedded", "external_service", "postgres", "expected"),
    [
        (False, False, False, "sqlite"),
        (True, False, True, "embedded"),
        (True, True, True, "embedded_service"),
        (False, False, True, "external"),
    ],
)
def test_the_mode_is_not_the_dialect(monkeypatch, embedded, external_service, postgres, expected):
    """DATABASE_URL=embedded is rewritten to a postgresql:// URL, so
    is_postgres() cannot tell these apart on its own."""
    monkeypatch.setattr(db_health.settings, "embedded_postgres", embedded, raising=False)
    monkeypatch.setattr(db_health.settings, "embedded_pg_external_service", external_service, raising=False)
    monkeypatch.setattr(db_health, "is_postgres", lambda: postgres)
    assert db_health.mode() == expected


@pytest.mark.asyncio
async def test_a_failing_probe_names_itself_and_does_not_take_the_others_down(db_session, monkeypatch):
    async def boom(_db):
        raise RuntimeError("relation does not exist")

    monkeypatch.setattr(db_health, "probe_size_bytes", boom)
    out = await db_health.collect(db_session)

    assert out["size_bytes"] is None
    assert "size_bytes" in out["probes_failed"]
    # …and everything else still answered.
    assert out["engine"] in ("SQLite", "PostgreSQL")
    assert out["version"]
    assert out["pool"] is not None


@pytest.mark.asyncio
async def test_every_probe_can_fail_at_once_and_the_call_still_returns(db_session, monkeypatch):
    async def boom(_db):
        raise RuntimeError("nope")

    for name in (
        "probe_version",
        "probe_size_bytes",
        "probe_sqlite",
        "probe_postgres",
        "probe_largest_tables",
        "probe_scans",
    ):
        monkeypatch.setattr(db_health, name, boom)
    out = await db_health.collect(db_session)
    assert set(out["probes_failed"]) >= {"version", "size_bytes", "sqlite", "postgres"}
    assert out["engine"]  # the one thing that cannot fail


@pytest.mark.asyncio
async def test_sqlite_reports_the_pragmas_the_app_actually_sets(db_session):
    out = await db_health.collect(db_session)
    if out["engine"] != "SQLite":
        pytest.skip("dialect-specific")
    assert out["sqlite"]["page_size"] > 0
    assert "freelist_count" in out["sqlite"]
    assert "wal_bytes" in out["sqlite"]
    assert out["postgres"] is None


@pytest.mark.asyncio
async def test_instrumentation_off_is_said_out_loud(db_session):
    """An empty table with no explanation reads as «no slow queries», which is a
    different claim from «nobody was counting»."""
    out = await db_health.collect(db_session)
    assert out["instrumentation"]["query_threshold_ms"] == 0
    assert out["instrumentation"]["slowest"] == []
    assert "slow_query_ms" in (out["instrumentation"]["reason"] or "")


@pytest.mark.asyncio
async def test_the_in_process_table_feeds_the_panel_on_sqlite(db_session):
    """The whole reason query_timing keeps its own table: SQLite has no
    pg_stat_statements."""
    qt.set_query_threshold_ms(100)
    qt.record(qt.fingerprint("SELECT 1 FROM spools"), 250.0)
    out = await db_health.collect(db_session)
    if out["engine"] != "SQLite":
        pytest.skip("dialect-specific")
    assert out["instrumentation"]["source"] == "in_process"
    assert out["instrumentation"]["slowest"][0]["statement"] == "SELECT 1 FROM spools"
    assert out["instrumentation"]["slowest"][0]["mean_ms"] == pytest.approx(250.0)
    assert out["instrumentation"]["reason"] is None


@pytest.mark.asyncio
async def test_statement_text_is_redacted_before_it_leaves(db_session):
    qt.set_query_threshold_ms(1)
    qt.record(qt.fingerprint("SELECT 'postgresql://bamdude:hunter2@host/db'"), 5.0)
    out = await db_health.collect(db_session)
    assert all("hunter2" not in row["statement"] for row in out["instrumentation"]["slowest"])
