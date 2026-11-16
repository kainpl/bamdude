"""Real CLI lock, transition and claim; full schema phases have bounded stubs."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from backend.app.core import (
    database,
    schema_repair,
    schema_repair_cli as cli,
    schema_repair_state as state,
    schema_spoolman_retry as compatibility,
)


def transition_pair():
    return json.loads(
        (Path(__file__).parents[3] / "app/data/pg_schema_spoolman_manifest_transition_v1.json").read_text(
            encoding="utf-8"
        )
    )


def test_known_transition_targets_the_installed_full_plan():
    database.import_all_models()
    assert state.manifest_hash(database.Base.metadata) == transition_pair()["to"], (
        "The final composition must explicitly retire this transition or regenerate it with an old-target proof."
    )


@pytest.mark.parametrize("old_manifest", [True, False], ids=["known-old", "already-current"])
def test_real_cli_retry_transitions_under_lock_before_claim(tmp_path, monkeypatch, old_manifest):
    from backend.app import migrations

    database.import_all_models()
    pair = transition_pair()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'repair.db'}", poolclass=NullPool)
    events = []

    async def seed():
        async with engine.begin() as conn:
            debt = await state.write_pending(conn, database.Base.metadata)
            debt["automatic_attempts_remaining"] = 0
            if old_manifest:
                debt["manifest"] = pair["from"]
            await state.put_record(conn, state.DEBT, debt)
            return debt

    original_debt = asyncio.run(seed())
    original_transition = compatibility.prepare_known_retry
    original_claim = schema_repair.claim

    async def transition(conn, metadata):
        # A second process must fail at the REAL installation owner.lock;
        # it may not get as far as the read-only status DB operation.
        probe = subprocess.run(
            [sys.executable, "-m", "backend.app.cli", "schema_repair", "status"],
            env={
                **os.environ,
                "BAMDUDE_IGNORE_DOTENV": "1",
                "DATA_DIR": str(tmp_path),
                "DATABASE_URL": str(engine.url),
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert probe.returncode == 2 and "Stop BamDude" in probe.stderr
        assert events == []
        changed = await original_transition(conn, metadata)
        assert changed is old_manifest
        current = await state.read_record(conn, state.DEBT)
        assert current == {**original_debt, "manifest": pair["to"]}
        events.append("transition")
        return changed

    async def claim(*args, **kwargs):
        assert events == ["transition"]
        claimed = await original_claim(*args, **kwargs)
        assert kwargs == {"automatic": False}
        assert claimed["manifest"] == pair["to"] and claimed["automatic_attempts_remaining"] == 0
        events.append("claim")
        return claimed

    async def migrations_stub(*_args):
        assert events == ["transition", "claim"]
        events.append("migrations")

    async def verified_phase_stub(_engine, _metadata, debt):
        assert events == ["transition", "claim", "migrations"]
        async with engine.begin() as conn:
            await state.finish_attempt(conn, debt, [])
        events.append("verified")

    async def residuals_stub(*_args):
        assert events == ["transition", "claim", "migrations", "verified"]
        events.append("residuals")
        return True

    monkeypatch.setattr(cli, "configuration", lambda: (tmp_path, engine.url))
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database.Base.metadata, "create_all", lambda _conn: None)
    monkeypatch.setattr(compatibility, "prepare_known_retry", transition)
    monkeypatch.setattr(schema_repair, "claim", claim)
    monkeypatch.setattr(migrations, "_run_pending", migrations_stub)
    monkeypatch.setattr(schema_repair, "run_claimed", verified_phase_stub)
    monkeypatch.setattr(schema_repair, "retry_residuals", residuals_stub)
    assert cli.main(["retry"]) == 0
    assert events == ["transition", "claim", "migrations", "verified", "residuals"]

    async def check():
        async with engine.connect() as conn:
            assert await state.read_record(conn, state.DEBT) is None
        await engine.dispose()

    asyncio.run(check())
