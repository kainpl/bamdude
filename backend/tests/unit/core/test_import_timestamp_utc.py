"""CSV imports normalize instants; portable NULL fallbacks use UTC only."""

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Column, DateTime, Integer, MetaData, Table, func, text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core import db_portable
from backend.app.services.git_restore import _parse_dt
from backend.app.services.spool_csv import _parse_datetime

UTC_NOW = datetime(2026, 6, 1, 10, 30)
EXISTING = datetime(2025, 8, 2, 3, 4, 5)


class _DatetimeMeta(type):
    def __instancecheck__(cls, instance):
        return isinstance(instance, datetime)


class NonUtcClock(datetime, metaclass=_DatetimeMeta):
    """A host at UTC+05:30; requesting UTC returns the same absolute instant."""

    @classmethod
    def now(cls, tz=None):
        instant = UTC_NOW.replace(tzinfo=timezone.utc)
        if tz is None:
            return instant.astimezone(timezone(timedelta(hours=5, minutes=30))).replace(tzinfo=None)
        return instant.astimezone(tz)

    @classmethod
    def fromisoformat(cls, value):
        # Only now() is simulated; keep parser values as real DBAPI datetimes.
        return datetime.fromisoformat(value)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-06-01T12:30:00+02:00", UTC_NOW),
        ("2026-06-01T07:00:00-03:30", UTC_NOW),
        ("2026-06-01T10:30:00Z", UTC_NOW),
        ("2026-06-01T10:30:00", UTC_NOW),
        (" 2026-06-01T12:30:00+02:00 ", UTC_NOW),
        ("not-a-date", None),
        ("", None),
    ],
)
def test_csv_normalizes_and_git_parser_keeps_existing_contract(raw, expected):
    assert _parse_datetime(raw) == expected
    assert _parse_dt(raw.strip()) == expected
    if expected is not None:
        assert _parse_datetime(raw).tzinfo is None


@pytest.mark.parametrize("raw", ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"])
def test_csv_rejects_an_instant_outside_the_datetime_range(raw):
    assert _parse_datetime(raw) is None


def timestamp_metadata():
    metadata = MetaData()
    Table(
        "sample",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("stamp", DateTime, nullable=False, server_default=func.current_timestamp()),
    )
    return metadata


def nullable_source(path, *, migrations):
    with closing(sqlite3.connect(path)) as source:
        source.executescript("CREATE TABLE sample(id INTEGER PRIMARY KEY, stamp DATETIME DEFAULT CURRENT_TIMESTAMP);")
        source.execute("INSERT INTO sample VALUES (1, NULL)")
        source.execute("INSERT INTO sample VALUES (2, ?)", (EXISTING.isoformat(" "),))
        if migrations:
            source.execute(
                "CREATE TABLE _migrations(id INTEGER PRIMARY KEY, version INTEGER NOT NULL UNIQUE, name TEXT NOT NULL, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
            )
        source.commit()


async def test_portable_export_fallback_is_utc_without_shifting_existing_values(tmp_path, monkeypatch):
    source, output = tmp_path / "source.db", tmp_path / "portable.db"
    nullable_source(source, migrations=True)
    monkeypatch.setattr(db_portable, "datetime", NonUtcClock)
    engine = create_async_engine(f"sqlite+aiosqlite:///{source}")
    try:
        await db_portable._export_pg_to_sqlite(engine, timestamp_metadata(), output)
    finally:
        await engine.dispose()
    with closing(sqlite3.connect(output)) as result:
        values = [datetime.fromisoformat(r[0]) for r in result.execute("SELECT stamp FROM sample ORDER BY id")]
    assert values == [UTC_NOW, EXISTING]


async def test_portable_import_defensive_fallback_is_utc(tmp_path, monkeypatch):
    source = tmp_path / "legacy.db"
    nullable_source(source, migrations=False)
    original = db_portable._reflect_sqlite_schema

    def reflected_nn(path, metadata):
        # Valid NN files cannot contain NULL after integrity_check; exercise the
        # defensive converter using this explicit, controlled metadata seam.
        reflected = original(path, metadata)
        reflected.tables["sample"].c.stamp.nullable = False
        return reflected

    monkeypatch.setattr(db_portable, "_reflect_sqlite_schema", reflected_nn)
    monkeypatch.setattr(db_portable, "datetime", NonUtcClock)
    monkeypatch.setattr("backend.app.core.db_dialect.is_postgres", lambda: False)
    monkeypatch.setattr(db_portable, "_conform_pending", False)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'target.db'}")
    try:
        assert await db_portable.import_sqlite_to_postgres(engine, timestamp_metadata(), source) == 1
        async with engine.connect() as connection:
            values = (await connection.execute(text("SELECT stamp FROM sample ORDER BY id"))).scalars().all()
        assert [datetime.fromisoformat(v) for v in values] == [UTC_NOW, EXISTING]
    finally:
        await engine.dispose()
