import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations.m169_auto_queue_filament_routing import upgrade
from backend.app.migrations.m174_auto_queue_base_material_match import upgrade as upgrade_base_material_match
from backend.app.services.filament_policy import (
    auto_policy,
    choices_policy,
    deserialize_policy,
    feed_policy,
    queue_policy,
    serialize_policy,
)
from backend.app.services.filament_routing import RoutingPolicy
from backend.app.services.printer_feed_snapshot import FeedSource, PrinterFeedSnapshot


@pytest.mark.parametrize(
    "use_ams,explicit,expected",
    [
        (False, None, "external_only"),
        (True, None, "auto"),
        (False, "auto", "auto"),
        (True, "external_only", "external_only"),
    ],
)
def test_legacy_feed_policy(use_ams, explicit, expected):
    assert feed_policy(explicit, use_ams) == expected


def test_manual_mapping_outranks_legacy_boolean_and_captures_selected_color():
    snapshot = PrinterFeedSnapshot(
        1, "P1P", True, 1, "r", True, True, True, (FeedSource(0, "ams", "PLA", "FF0000", "GFA00", (0,)),)
    )
    policy = choices_policy({"use_ams": False, "ams_mapping": [0, -1]}, snapshot)
    assert policy.mode == "pinned"
    assert policy.feed_policy == "auto"
    assert policy.physical_pins == {
        1: {"source_id": 0, "type": "PLA", "color": "FF0000", "tray_info_idx": "GFA00", "nozzles": [0]}
    }
    assert deserialize_policy(serialize_policy(policy, library_file_id=7, plate_id=15, printer_id=1)) == policy


def test_auto_global_color_policy_survives_without_overrides():
    item = SimpleNamespace(
        use_ams=True,
        filament_overrides=None,
        force_color_match=True,
        allow_base_material_match=True,
    )
    policy = auto_policy(item)
    assert policy.force_color_match
    assert policy.allow_base_material_match
    restored = deserialize_policy(serialize_policy(policy))
    assert restored.force_color_match
    assert restored.allow_base_material_match


@pytest.mark.parametrize(
    "value",
    [
        "broken",
        "[]",
        '{"version":2}',
        '{"version":1,"mode":"pinned","feed_policy":"auto","physical_pins":{"1":{"source_id":"bad"}}}',
    ],
)
def test_unknown_or_malformed_snapshot_is_closed(value):
    assert deserialize_policy(value).review_required


def test_old_pinned_row_without_evidence_requires_review():
    assert queue_policy(SimpleNamespace(filament_routing=None, ams_mapping=None, use_ams=True)).review_required
    assert not RoutingPolicy().force_color_match


@pytest.mark.asyncio
async def test_migration_is_database_only_and_repeatable(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}")
    async with engine.begin() as conn:
        await assert_migration_contract(conn)
    await engine.dispose()


async def assert_migration_contract(conn):
    await conn.execute(
        text(
            "CREATE TABLE auto_queue_items (id INTEGER PRIMARY KEY, use_ams BOOLEAN, force_color_match BOOLEAN, filament_overrides TEXT)"
        )
    )
    await conn.execute(text("CREATE TABLE printer_queues (id INTEGER PRIMARY KEY, printer_id INTEGER)"))
    await conn.execute(
        text(
            "CREATE TABLE print_queue (id INTEGER PRIMARY KEY, queue_id INTEGER, archive_id INTEGER, library_file_id INTEGER, plate_id INTEGER, ams_mapping TEXT, source_auto_item_id INTEGER)"
        )
    )
    await conn.execute(text("INSERT INTO printer_queues VALUES (3,7)"))
    await conn.execute(text("INSERT INTO auto_queue_items VALUES (1,false,true,NULL)"))
    await conn.execute(
        text(
            "INSERT INTO print_queue VALUES (1,3,NULL,6,15,'[0]',1), (2,3,9,NULL,NULL,'[0]',NULL), (3,3,NULL,7,NULL,NULL,NULL)"
        )
    )
    await upgrade(conn)
    values = (await conn.execute(text("SELECT filament_routing FROM print_queue ORDER BY id"))).scalars().all()
    auto, pinned, unknown = [json.loads(v) for v in values]
    assert auto["mode"] == "auto" and auto["force_color_match"] and auto["feed_policy"] == "external_only"
    assert auto["resolved_plate_id"] == 15 and auto["printer_id"] == 7
    assert pinned["mode"] == "pinned" and pinned["physical_pins"] == {"1": {"source_id": 0}}
    assert unknown["review_required"]
    await conn.execute(text("UPDATE auto_queue_items SET feed_policy = 'auto' WHERE id = 1"))
    await upgrade(conn)
    await upgrade_base_material_match(conn)
    assert (await conn.execute(text("SELECT feed_policy FROM auto_queue_items WHERE id = 1"))).scalar_one() == "auto"
    assert (
        await conn.execute(text("SELECT allow_base_material_match FROM auto_queue_items WHERE id = 1"))
    ).scalar_one()
    await upgrade_base_material_match(conn)
    assert (await conn.execute(text("SELECT filament_routing FROM print_queue ORDER BY id"))).scalars().all() == values


@pytest.mark.parametrize(
    "override",
    [
        {"slot_id": 0},
        {"slot_id": 1, "color": 42},
        {"slot_id": 1, "type": []},
        {"slot_id": 1, "force_color_match": "false"},
    ],
)
def test_malformed_persisted_overrides_require_review(override):
    saved = json.loads(serialize_policy(RoutingPolicy()))
    saved["filament_overrides"] = [override]
    assert deserialize_policy(saved).review_required
    assert auto_policy(
        SimpleNamespace(use_ams=True, force_color_match=False, filament_overrides=[override])
    ).review_required
