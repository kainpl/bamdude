"""Retired audit hashes and unknown manifests never authorize debt adoption."""

import pytest
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core import schema_repair_state as state
from backend.app.core.schema_repair import claim


@pytest.mark.parametrize("automatic", [True, False])
@pytest.mark.parametrize("manifest", ["retired-audit-plan", "unrelated-plan"])
async def test_unknown_manifest_keeps_debt_audit_and_budget_unchanged(tmp_path, automatic, manifest):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old-plan.db'}")
    try:
        async with engine.begin() as conn:
            debt = await state.write_pending(conn, MetaData(), acceptance={"test": "preserved"})
            debt.update(manifest=manifest, automatic_attempts_remaining=0, last_failure="unverified_schema_objects")
            await state.put_record(conn, state.DEBT, debt)
            audit = await state.read_raw(conn, debt["acceptance"])
            residuals = await state.read_raw(conn, state.RESIDUALS)
        with pytest.raises(state.SchemaRepairPending, match="unknown_manifest"):
            await claim(engine, MetaData(), automatic=automatic)
        async with engine.connect() as conn:
            assert await state.read_raw(conn, state.DEBT) == state.encode(debt)
            assert await state.read_raw(conn, debt["acceptance"]) == audit
            assert await state.read_raw(conn, state.RESIDUALS) == residuals
    finally:
        await engine.dispose()
