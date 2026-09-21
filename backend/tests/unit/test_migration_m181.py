"""Old effective heating limits survive the switch; DEBUG reruns do not undo user edits."""

import json
from itertools import product

import pytest
from sqlalchemy import text

from backend.app.migrations import m181_stagger_group_overrides as migration
from backend.app.services.stagger_groups import StaggerGroupResolver, StaggerSplit, parse_limit_map

pytestmark = pytest.mark.asyncio


async def _put(conn, values):
    for key, value in values.items():
        await conn.execute(
            text("INSERT INTO settings (key, value) VALUES (:key, :value)"), {"key": key, "value": value}
        )


async def _read(conn):
    return dict((await conn.execute(text("SELECT key, value FROM settings"))).all())


@pytest.mark.parametrize("base,expected", [("2", 2), ("5", 5), ("0", 1), ("-1", 1), ("", 2), (None, 2)])
async def test_preserves_every_old_intersection_and_inactive_override(test_engine, base, expected):
    tags = {1: 1, 2: 6, 3: 9}
    locations = {10: 1, 20: 4}
    values = {"stagger_tag_limits": json.dumps(tags), "stagger_location_limits": json.dumps(locations)}
    if base is not None:
        values["stagger_concurrent"] = base
    async with test_engine.begin() as conn:
        await _put(conn, values)
        await migration.upgrade(conn)
        after = await _read(conn)
        tag_limits = parse_limit_map(after["stagger_tag_limits"])
        location_limits = parse_limit_map(after["stagger_location_limits"])
        assert tag_limits == {k: min(v, expected) for k, v in tags.items()}
        assert location_limits == {k: min(v, expected) for k, v in locations.items()}
        # Migration also protects disabled axes. Later enabling either/both
        # must preserve the old cap, including entries with no override.
        for by_tags, by_location in product((False, True), repeat=2):
            resolver = StaggerGroupResolver(
                StaggerSplit(
                    by_tags=by_tags,
                    tag_ids=frozenset({1, 2, 3, 4}),
                    tag_limits=tag_limits,
                    by_location=by_location,
                    location_ids=frozenset({10, 20, 30}),
                    location_limits=location_limits,
                ),
                tags_by_printer={},
                tag_names={i: str(i) for i in (1, 2, 3, 4)},
                location_by_printer={},
                parent_by_location=dict.fromkeys((10, 20, 30)),
                location_names={i: str(i) for i in (10, 20, 30)},
            )
            for tag, location in resolver.universe:
                old = min(expected, tags.get(tag, expected), locations.get(location, expected))
                assert resolver.cap_for((tag, location), expected) == old


async def test_rerun_after_user_raises_cap_is_a_noop(test_engine):
    async with test_engine.begin() as conn:
        await _put(conn, {"stagger_concurrent": "2", "stagger_tag_limits": '{"1":6}'})
        await migration.upgrade(conn)
    async with test_engine.begin() as conn:
        await conn.execute(
            text("UPDATE settings SET value = :value WHERE key = :key"),
            {"key": "stagger_tag_limits", "value": '{"1":6}'},
        )
    async with test_engine.begin() as conn:
        await migration.upgrade(conn)
        assert (await _read(conn))["stagger_tag_limits"] == '{"1":6}'


async def test_fresh_install_marks_conversion_before_any_overrides_exist(test_engine):
    async with test_engine.begin() as conn:
        await migration.upgrade(conn)
        await _put(conn, {"stagger_tag_limits": '{"1":6}'})
        await migration.upgrade(conn)
        assert (await _read(conn))["stagger_tag_limits"] == '{"1":6}'


@pytest.mark.parametrize("raw", ["not json", "[]", "null", '{"x":99,"1":true,"2":"9","3":0,"4":-1}'])
async def test_invalid_maps_or_entries_remain_ignored(test_engine, raw):
    async with test_engine.begin() as conn:
        await _put(conn, {"stagger_tag_limits": raw})
        await migration.upgrade(conn)
        assert (await _read(conn))["stagger_tag_limits"] == raw


async def test_invalid_base_aborts_without_guessing_or_marking_complete(test_engine):
    async with test_engine.begin() as conn:
        await _put(conn, {"stagger_concurrent": "broken", "stagger_tag_limits": '{"1":6}'})
    with pytest.raises(ValueError):
        async with test_engine.begin() as conn:
            await migration.upgrade(conn)
    async with test_engine.begin() as conn:
        saved = await _read(conn)
        assert saved == {"stagger_concurrent": "broken", "stagger_tag_limits": '{"1":6}'}


async def test_marker_and_rewrite_rollback_together(test_engine):
    async with test_engine.begin() as conn:
        await _put(conn, {"stagger_tag_limits": '{"1":6}'})
    with pytest.raises(RuntimeError):
        async with test_engine.begin() as conn:
            await migration.upgrade(conn)
            raise RuntimeError("interrupted before commit")
    async with test_engine.begin() as conn:
        assert await _read(conn) == {"stagger_tag_limits": '{"1":6}'}
        await migration.upgrade(conn)
        assert json.loads((await _read(conn))["stagger_tag_limits"]) == {"1": 2}
