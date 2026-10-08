"""m195 and the models: plate_renders / plate_render_objects, the part-renders root (plan E3, task 19)."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

INSERT = (
    "INSERT INTO plate_renders (file_sha256, plate_index, renderer_version, status, phase, priority, attempts,"
    " next_attempt_at, requested_at) VALUES (:sha, 1, 2, :status, 'render', 0, 0, '2026-01-01', '2026-01-01')"
)


@pytest.fixture
async def bare():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    yield engine
    await engine.dispose()


async def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in (await conn.exec_driver_sql(f"PRAGMA table_info({table})")).all()}


async def test_the_migration_creates_both_tables_and_is_idempotent(bare):
    from backend.app.migrations import m195_part_renders as m195

    async with bare.begin() as conn:
        await m195.upgrade(conn)
        await m195.upgrade(conn)  # DEBUG=true re-runs the newest migration on every start
        assert {"file_sha256", "plate_index", "renderer_version", "phase", "orphaned_at"} <= await _columns(
            conn, "plate_renders"
        )
        assert {"render_id", "identify_id", "method", "tools"} <= await _columns(conn, "plate_render_objects")


@pytest.mark.parametrize(
    ("sha", "status"), [("a" * 64, "pending"), ("b" * 63, "pending"), ("c" * 64, "rendering")], ids=str
)
async def test_the_key_and_the_checks_hold(bare, sha, status):
    from backend.app.migrations import m195_part_renders as m195

    async with bare.begin() as conn:
        await m195.upgrade(conn)
        await conn.execute(text(INSERT), {"sha": "a" * 64, "status": "pending"})
    with pytest.raises(IntegrityError):  # a second row of the key, a short hash, a status outside the four
        async with bare.begin() as conn:
            await conn.execute(text(INSERT), {"sha": sha, "status": status})


@pytest.mark.parametrize("table", ["plate_renders", "plate_render_objects"])
async def test_the_model_and_the_migration_have_the_same_columns(bare, test_engine, table):
    from backend.app.migrations import m195_part_renders as m195

    async with bare.begin() as conn:
        await m195.upgrade(conn)
        migrated = await _columns(conn, table)
    async with test_engine.begin() as conn:
        created = await _columns(conn, table)
    assert migrated == created


def test_the_part_renders_root_is_derived_from_data_dir():
    from backend.app.core.config import settings

    assert settings.part_renders_dir == settings.data_dir / "part-renders"


def test_the_root_is_in_the_data_map_and_has_a_storage_rule():
    from backend.app.api.routes import system
    from backend.app.core.config import settings

    assert settings.part_renders_dir in system._get_data_dirs()
    key, _ = system._classify_file(settings.part_renders_dir / "aa" / "x.lg.png", system._get_storage_rules())
    assert key == "part_renders"


def test_the_root_is_not_backed_up():
    """Spec §8.3: derived files, rebuilt by the queue; the deliberate exception to one-root-per-subsystem."""
    from backend.app.core.config import settings
    from backend.app.services import backup_files

    root = settings.part_renders_dir
    assert not any(root == path or path in root.parents for path in backup_files.directories(settings).values())
