"""Durable repair ownership on a real file, across engine/process boundaries."""

import json

import pytest
from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core import schema_repair_state as state


@pytest.fixture
async def engine(tmp_path):
    result = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'repair.db'}")
    yield result
    await result.dispose()


async def test_kills_do_not_refund_attempts_and_explicit_retry_does_not_refill(engine):
    metadata = MetaData()
    async with engine.begin() as conn:
        debt = await state.write_pending(conn, metadata)
    for remaining in (1, 0):
        async with engine.begin() as conn:
            claimed = await state.claim_attempt(conn, metadata)
            assert claimed["id"] == debt["id"]
            assert claimed["automatic_attempts_remaining"] == remaining
        # A killed owner cannot execute a finally block. Reopen independently.
        await engine.dispose()
    with pytest.raises(state.SchemaRepairPending, match="manual_retry_required"):
        async with engine.begin() as conn:
            await state.claim_attempt(conn, metadata)
    async with engine.begin() as conn:
        assert (await state.claim_attempt(conn, metadata, automatic=False))["automatic_attempts_remaining"] == 0


async def test_failed_attempt_exhausts_budget_without_clearing_debt(engine):
    async with engine.begin() as conn:
        await state.write_pending(conn, MetaData())
        claimed = await state.claim_attempt(conn, MetaData())
    async with engine.begin() as conn:
        await state.fail_attempt(conn, claimed, "technical_failure")
    async with engine.connect() as conn:
        debt = await state.read_record(conn, state.DEBT)
        assert debt["automatic_attempts_remaining"] == 0
        assert debt["last_failure"] == "technical_failure"


async def test_replacement_rollback_cannot_publish_debt_or_audit(engine):
    with pytest.raises(RuntimeError, match="replacement failed"):
        async with engine.begin() as conn:
            await state.write_pending(conn, MetaData(), acceptance={"actor": "local-authority"})
            raise RuntimeError("replacement failed")
    async with engine.connect() as conn:
        assert await state.read_record(conn, state.DEBT) is None
        assert (await conn.execute(text('SELECT "key" FROM settings'))).all() == []


async def test_old_owner_cannot_clear_new_debt_or_publish_its_residuals(engine):
    async with engine.begin() as conn:
        old = await state.write_pending(conn, MetaData())
    async with engine.begin() as conn:
        new = await state.write_pending(conn, MetaData())
    with pytest.raises(state.SchemaRepairPending, match="debt_changed"):
        async with engine.begin() as conn:
            await state.finish_attempt(conn, old, [{"object": "old", "count": 3}])
    async with engine.connect() as conn:
        assert await state.read_record(conn, state.DEBT) == new
        assert (await state.read_record(conn, state.RESIDUALS))["entries"] == {}


async def test_complete_reverification_replaces_stale_residuals_and_clears_exact_owner(engine):
    async with engine.begin() as conn:
        debt = await state.write_pending(conn, MetaData())
        await state.put_record(conn, state.RESIDUALS, {"version": 1, "entries": {"formerly-broken": {"count": 3}}})
    async with engine.begin() as conn:
        await state.finish_attempt(conn, debt, [{"object": "still-broken", "count": 2}])
    async with engine.connect() as conn:
        assert await state.read_record(conn, state.DEBT) is None
        assert (await state.read_record(conn, state.RESIDUALS))["entries"] == {
            "still-broken": {"object": "still-broken", "count": 2}
        }


@pytest.mark.parametrize("edit", ["extra", "version", "manifest", "budget", "audit", "malformed"])
async def test_unknown_contract_never_spends_or_clears_debt(engine, edit):
    async with engine.begin() as conn:
        debt = await state.write_pending(conn, MetaData())
        if edit == "extra":
            debt["skip_verification"] = True
        elif edit == "version":
            debt["version"] = True
        elif edit == "manifest":
            debt["manifest"] = "untrusted-backup-plan"
        elif edit == "budget":
            debt["automatic_attempts_remaining"] = True
        elif edit == "audit":
            debt["acceptance"] = "unrelated-approval"
        raw = "{invalid" if edit == "malformed" else json.dumps(debt)
        await conn.execute(text('UPDATE settings SET value=:raw WHERE "key"=:key'), {"raw": raw, "key": state.DEBT})
    with pytest.raises(state.SchemaRepairPending):
        async with engine.begin() as conn:
            await state.claim_attempt(conn, MetaData())
    async with engine.connect() as conn:
        assert await state.read_raw(conn, state.DEBT) == raw


async def test_imported_acceptance_cannot_be_reused_as_new_owner(engine):
    async with engine.begin() as conn:
        first = await state.write_pending(conn, MetaData(), acceptance={"actor": "original"})
        second = await state.write_pending(conn, MetaData())
    assert second["id"] != first["id"] and second["acceptance"] is None
