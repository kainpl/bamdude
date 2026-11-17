"""The one known plan upgrade is manual, exact and retains all ownership."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core import schema_repair_state as state, schema_spoolman_retry as compatibility
from backend.app.core.schema_repair import claim


@pytest.fixture
async def engine(tmp_path):
    result = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'known-plan.db'}")
    yield result
    await result.dispose()


@pytest.fixture
def pair(monkeypatch):
    value = json.loads(
        (Path(__file__).parents[3] / "app/data/pg_schema_spoolman_manifest_transition_v1.json").read_text(
            encoding="utf-8"
        )
    )
    # Exercise the fixed transition even once later models supersede this plan.
    # The real PG/CLI probe checks these hashes against the actual installed plans.
    monkeypatch.setattr(state, "manifest_hash", lambda _metadata: value["to"])
    monkeypatch.setattr(compatibility, "manifest_hash", lambda _metadata: value["to"])
    return value


async def old_debt(conn, pair):
    debt = await state.write_pending(conn, MetaData(), acceptance={"test": "preserved"})
    debt.update(manifest=pair["from"], automatic_attempts_remaining=0, last_failure="unverified_schema_objects")
    await state.put_record(conn, state.DEBT, debt)
    return debt


async def test_automatic_start_refuses_then_manual_upgrade_retains_owner_audit_budget(engine, pair):
    async with engine.begin() as conn:
        previous = await old_debt(conn, pair)
        audit = await state.read_record(conn, previous["acceptance"])
    with pytest.raises(state.SchemaRepairPending, match="unknown_manifest"):
        await claim(engine, MetaData())
    async with engine.begin() as conn:
        assert await state.read_record(conn, state.DEBT) == previous
        assert await compatibility.prepare_known_retry(conn, MetaData())
        current = await state.read_record(conn, state.DEBT)
        assert current == {**previous, "manifest": pair["to"]}
        assert await state.read_record(conn, previous["acceptance"]) == audit
        assert not await compatibility.prepare_known_retry(conn, MetaData())
    with pytest.raises(state.SchemaRepairPending, match="debt_changed"):
        async with engine.begin() as conn:
            await state.finish_attempt(conn, previous, [])
    async with engine.begin() as conn:
        claimed = await state.claim_attempt(conn, MetaData(), automatic=False)
        assert claimed["id"] == previous["id"] and claimed["automatic_attempts_remaining"] == 0


@pytest.mark.parametrize("different", ["source", "target"])
async def test_unknown_source_or_different_installed_plan_is_not_adopted(engine, pair, monkeypatch, different):
    async with engine.begin() as conn:
        previous = await old_debt(conn, pair)
        if different == "source":
            previous["manifest"] = "unrelated-plan"
            await state.put_record(conn, state.DEBT, previous)
        else:
            monkeypatch.setattr(compatibility, "manifest_hash", lambda _: "different-installed-models")
            monkeypatch.setattr(state, "manifest_hash", lambda _: "different-installed-models")
        assert not await compatibility.prepare_known_retry(conn, MetaData())
        assert await state.read_record(conn, state.DEBT) == previous


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "not-a-uuid"),
        ("version", True),
        ("automatic_attempts_remaining", True),
        ("last_failure", 2),
        ("acceptance", "foreign-audit"),
        ("extra", "forged"),
    ],
)
async def test_known_hash_never_excuses_invalid_ownership_metadata(engine, pair, field, value):
    async with engine.begin() as conn:
        previous = await old_debt(conn, pair)
        previous[field] = value
        await state.put_record(conn, state.DEBT, previous)
    with pytest.raises(state.SchemaRepairPending):
        async with engine.begin() as conn:
            await compatibility.prepare_known_retry(conn, MetaData())
    async with engine.begin() as conn:
        assert await state.read_raw(conn, state.DEBT) == state.encode(previous)


async def test_changed_owner_is_never_overwritten_between_read_and_cas(engine, pair, monkeypatch):
    async with engine.begin() as conn:
        previous = await old_debt(conn, pair)
    original_read = compatibility.read_record

    async def changed_before_update(conn, key):
        old = await original_read(conn, key)
        await state.put_record(conn, key, {**old, "id": str(uuid4()), "acceptance": None})
        return old

    monkeypatch.setattr(compatibility, "read_record", changed_before_update)
    with pytest.raises(state.SchemaRepairPending, match="debt_changed"):
        async with engine.begin() as conn:
            await compatibility.prepare_known_retry(conn, MetaData())
    async with engine.connect() as conn:
        assert await state.read_record(conn, state.DEBT) == previous
