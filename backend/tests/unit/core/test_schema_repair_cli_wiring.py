"""Real offline CLI ownership and claim precede every mutating schema phase."""

import asyncio
import os
import subprocess
import sys

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from backend.app.core import database, schema_repair, schema_repair_cli as cli, schema_repair_state as state

# An unreleased audit plan deliberately retired by the final composition.
RETIRED_MANIFEST = "fdf05bc2fce41028e6cc47b0aa4fb8833c1f4e7d5aae8957962b57372d58dbae"


@pytest.mark.parametrize("old_manifest", [False, True], ids=["current", "retired-audit-plan"])
def test_real_cli_retry_holds_owner_lock_and_claims_before_ddl(tmp_path, monkeypatch, old_manifest):
    from backend.app import migrations

    database.import_all_models()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'repair.db'}", poolclass=NullPool)
    events, probes = [], []

    async def seed():
        async with engine.begin() as conn:
            debt = await state.write_pending(conn, database.Base.metadata)
            debt["automatic_attempts_remaining"] = 0
            if old_manifest:
                debt["manifest"] = RETIRED_MANIFEST
            await state.put_record(conn, state.DEBT, debt)
            return debt

    original_debt = asyncio.run(seed())
    original_claim = schema_repair.claim

    async def claim(*args, **kwargs):
        # A child with valid config must fail at owner.lock. Reaching status
        # instead has a distinctive exit/marker, not an ambiguous error string.
        probe_code = """
from pathlib import Path
from sqlalchemy import make_url
from backend.app.core import schema_repair_cli as cli
import os
cli.configuration = lambda: (Path(os.environ['DATA_DIR']), make_url(os.environ['DATABASE_URL']))
async def status(url):
    print('STATUS_REACHED_DATABASE')
    return 77
cli.status = status
raise SystemExit(cli.main(['status']))
"""
        probes.append(
            subprocess.run(
                [sys.executable, "-c", probe_code],
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
        )
        assert events == []
        events.append("claim")
        return await original_claim(*args, **kwargs)

    def create_tables(_conn):
        assert events == ["claim"]
        events.append("create_all")

    async def migrations_stub(*_args):
        assert events == ["claim", "create_all"]
        events.append("migrations")

    async def verified_phase_stub(_engine, _metadata, debt):
        assert events == ["claim", "create_all", "migrations"]
        async with engine.begin() as conn:
            await state.finish_attempt(conn, debt, [])
        events.append("verified")

    async def residuals_stub(*_args):
        assert events == ["claim", "create_all", "migrations", "verified"]
        events.append("residuals")
        return True

    monkeypatch.setattr(cli, "configuration", lambda: (tmp_path, engine.url))
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database.Base.metadata, "create_all", create_tables)
    monkeypatch.setattr(schema_repair, "claim", claim)
    monkeypatch.setattr(migrations, "_run_pending", migrations_stub)
    monkeypatch.setattr(schema_repair, "run_claimed", verified_phase_stub)
    monkeypatch.setattr(schema_repair, "retry_residuals", residuals_stub)
    result = cli.main(["retry"])
    assert len(probes) == 1
    assert probes[0].returncode == 2, (probes[0].stdout, probes[0].stderr)
    assert "STATUS_REACHED_DATABASE" not in probes[0].stdout
    assert "Stop BamDude" in probes[0].stderr
    assert result == (2 if old_manifest else 0)
    assert events == (["claim"] if old_manifest else ["claim", "create_all", "migrations", "verified", "residuals"])

    async def check():
        async with engine.connect() as conn:
            assert await state.read_record(conn, state.DEBT) == (original_debt if old_manifest else None)
        await engine.dispose()

    asyncio.run(check())
