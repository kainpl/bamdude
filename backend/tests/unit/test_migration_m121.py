"""m121 recovers bed temperature from the 3MFs still on disk.

Built against real exported files rather than a hand-written config blob: the
whole defect was a wrong assumption about which keys a 3MF actually contains, so
a fixture invented from the same assumption would confirm the bug rather than
the fix.
"""

import json
import zipfile

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m121_backfill_bed_temperature as m121


def _make_3mf(path, settings: dict) -> None:
    """A minimal 3MF: the entry name and layout match what BambuStudio writes."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/project_settings.config", json.dumps(settings))


def _bambu_settings(bed_type: str, **temps) -> dict:
    """Shaped like the real thing — one-element string arrays, one per extruder,
    and no ``bed_temperature`` key anywhere, because exports do not carry one."""
    data = {k: [str(v)] for k, v in temps.items()}
    data["curr_bed_type"] = bed_type
    data["nozzle_temperature"] = ["255"]
    return data


class _Factory:
    """Async-session factory shim: the migration takes one, not an engine."""

    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self._session


class _Session:
    """Just enough AsyncSession for m121: execute + commit, no ORM."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, *args, **kwargs):
        return await self._conn.execute(*args, **kwargs)

    async def commit(self):
        pass  # the caller's engine.begin() owns the transaction


async def _run(tmp_path, rows: list[tuple[int, str]], name: str):
    """Seed print_archives with (id, file_path) and run the backfill."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/{name}")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE print_archives (id INTEGER PRIMARY KEY, file_path TEXT, bed_temperature INTEGER)"
        )
        for row_id, path in rows:
            await conn.exec_driver_sql(
                "INSERT INTO print_archives (id, file_path, bed_temperature) VALUES (?, ?, NULL)",
                (row_id, path),
            )
        await m121.seed(_Factory(_Session(conn)))
        result = await conn.exec_driver_sql("SELECT id, bed_temperature FROM print_archives ORDER BY id")
        out = dict(result.fetchall())
    await engine.dispose()
    return out


@pytest.mark.asyncio
async def test_it_recovers_the_plates_own_temperature(tmp_path):
    """Values all differ, so reading the wrong key fails instead of coinciding."""
    f = tmp_path / "textured.3mf"
    _make_3mf(
        f,
        _bambu_settings(
            "Textured PEI Plate",
            cool_plate_temp=35,
            hot_plate_temp=55,
            textured_plate_temp=75,
            supertack_plate_temp=45,
        ),
    )

    assert await _run(tmp_path, [(1, str(f))], "a.db") == {1: 75}


@pytest.mark.asyncio
async def test_a_row_that_already_has_a_value_is_left_alone(tmp_path):
    """It was either written by the fixed parser or set by hand; the backfill
    exists to fill blanks, not to have opinions about existing data."""
    f = tmp_path / "hot.3mf"
    _make_3mf(f, _bambu_settings("High Temp Plate", hot_plate_temp=55))

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/b.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE print_archives (id INTEGER PRIMARY KEY, file_path TEXT, bed_temperature INTEGER)"
        )
        await conn.exec_driver_sql(
            "INSERT INTO print_archives (id, file_path, bed_temperature) VALUES (1, ?, 99)", (str(f),)
        )
        await m121.seed(_Factory(_Session(conn)))
        got = (await conn.exec_driver_sql("SELECT bed_temperature FROM print_archives WHERE id = 1")).scalar()
    await engine.dispose()

    assert got == 99


@pytest.mark.asyncio
async def test_a_missing_file_leaves_the_row_null(tmp_path):
    """Archives whose 3MF could never be downloaded carry file_path but no file.
    They stay NULL, exactly as they were — nothing to recover from."""
    assert await _run(tmp_path, [(1, str(tmp_path / "gone.3mf"))], "c.db") == {1: None}


@pytest.mark.asyncio
async def test_a_corrupt_file_does_not_abort_the_upgrade(tmp_path):
    """One unreadable archive must not stop the other thousand — and must not
    take the application's first start down with it."""
    broken = tmp_path / "broken.3mf"
    broken.write_bytes(b"this is not a zip")
    good = tmp_path / "good.3mf"
    _make_3mf(good, _bambu_settings("Cool Plate", cool_plate_temp=35))

    assert await _run(tmp_path, [(1, str(broken)), (2, str(good))], "d.db") == {1: None, 2: 35}


@pytest.mark.asyncio
async def test_a_3mf_without_plate_temperatures_stays_null(tmp_path):
    """Guessing here would invent data rather than recover it."""
    f = tmp_path / "bare.3mf"
    _make_3mf(f, {"nozzle_temperature": ["255"], "curr_bed_type": "Cool Plate"})

    assert await _run(tmp_path, [(1, str(f))], "e.db") == {1: None}


@pytest.mark.asyncio
async def test_it_is_idempotent(tmp_path):
    """DEBUG=true re-runs the latest migration on every start."""
    f = tmp_path / "again.3mf"
    _make_3mf(f, _bambu_settings("Supertack Plate", supertack_plate_temp=45))

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/f.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE print_archives (id INTEGER PRIMARY KEY, file_path TEXT, bed_temperature INTEGER)"
        )
        await conn.exec_driver_sql(
            "INSERT INTO print_archives (id, file_path, bed_temperature) VALUES (1, ?, NULL)", (str(f),)
        )
        await m121.seed(_Factory(_Session(conn)))
        await m121.seed(_Factory(_Session(conn)))  # must not raise
        got = (await conn.exec_driver_sql("SELECT bed_temperature FROM print_archives WHERE id = 1")).scalar()
    await engine.dispose()

    assert got == 45


def test_version_and_name():
    assert m121.version == 121
    assert m121.name == "backfill_bed_temperature"
