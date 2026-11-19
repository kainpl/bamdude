"""m247 turns the bare ``RRGGBBAA`` colours of no-3MF archives into ``#RRGGBB`` and nothing else."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.migrations import m247_no_3mf_archive_colors as m247


def test_identity():
    assert m247.version == 247
    assert m247.name == "no_3mf_archive_colors"


@pytest.mark.parametrize(
    ("stored", "repaired"),
    [
        ("FF8800FF", "#FF8800"),
        ("ff8800ff,00FF0080", "#FF8800,#00FF00"),
        # Six colours need 53 characters; the column kept 50 and cut the last one.
        ("11111111,22222222,33333333,44444444,55555555,66666", "#111111,#222222,#333333,#444444,#555555"),
        # Not the old form: left alone.
        ("#FF8800", None),
        ("#FF8800,#00FF00", None),
        ("#FF8800FF", None),
        ("FF8800", None),  # six bare digits are not what the printer sends
        ("FF8800FF,red", None),
        ("FF8800FF,00FF", None),  # a short token in a value the column did not cut
        ("", None),
        (None, None),
    ],
)
def test_only_the_old_mqtt_form_is_repaired(stored, repaired):
    assert m247.normalised(stored) == repaired


@pytest.mark.asyncio
async def test_seed_repairs_rows_once_and_leaves_the_rest(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm247.db'}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    rows = {
        1: "FF8800FF,00FF00FF",
        2: "#112233,#445566",
        3: None,
        4: "11111111,22222222,33333333,44444444,55555555,66666",
        5: "PLA red",
        6: "",
    }
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE print_archives (id INTEGER PRIMARY KEY, filament_color VARCHAR(50))"))
        for archive_id, color in rows.items():
            await conn.execute(
                text("INSERT INTO print_archives (id, filament_color) VALUES (:id, :color)"),
                {"id": archive_id, "color": color},
            )
        await m247.upgrade(conn)

    async def stored():
        async with engine.connect() as conn:
            return dict((await conn.execute(text("SELECT id, filament_color FROM print_archives"))).all())

    await m247.seed(factory)
    first = await stored()
    await m247.seed(factory)  # a second run finds nothing to repair

    assert first == await stored()
    assert first == {
        1: "#FF8800,#00FF00",
        2: "#112233,#445566",
        3: None,
        4: "#111111,#222222,#333333,#444444,#555555",
        5: "PLA red",
        6: "",
    }
    await engine.dispose()
