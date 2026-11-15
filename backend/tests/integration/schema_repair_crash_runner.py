"""Independent child for D4's real kill/restart gates, never a farm startup."""

import asyncio
import json
import os
import sys
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from backend.app.core import database
from backend.app.core.schema_index_repair import manifest, quote
from backend.app.core.schema_repair import claim, run_claimed
from backend.app.core.schema_repair_state import DEBT, claim_attempt, read_record, write_pending


async def prepare():
    await database.init_db()
    from backend.app.models.printer import Printer

    async with database.async_session() as session:
        if await session.scalar(text("SELECT count(*) FROM printers WHERE serial_number='D4-CRASH-PROBE'")) == 0:
            session.add(
                Printer(
                    name="D4 crash probe",
                    serial_number="D4-CRASH-PROBE",
                    ip_address="127.0.0.1",
                    access_code="synthetic",
                )
            )
            await session.commit()
    async with database.engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            for table, kind, column in (
                ("printers", "unique", "serial_number"),
                ("spool_usage_history", "foreign", "archive_id"),
            ):
                items = await conn.run_sync(
                    lambda s, table=table, kind=kind: (
                        inspect(s).get_unique_constraints(table)
                        if kind == "unique"
                        else inspect(s).get_foreign_keys(table)
                    )
                )
                field = "column_names" if kind == "unique" else "constrained_columns"
                matched = [r for r in items if r[field] == [column]]
                assert len(matched) == 1
                await conn.execute(text(f"ALTER TABLE {quote(table)} DROP CONSTRAINT {quote(matched[0]['name'])}"))
            await conn.execute(
                text("ALTER TABLE oidc_providers DROP CONSTRAINT ck_auto_link_requires_verified_email_claim")
            )
        for row in manifest():
            await conn.execute(text("DROP INDEX IF EXISTS " + quote(row["name"])))
        debt = await write_pending(conn, database.Base.metadata)
    return {"debt": debt["id"], "budget": 2}


async def kill_window(phase):
    database.import_all_models()

    async def observe(label):
        if label == phase:
            Path(os.environ["D4_SIGNAL"]).write_bytes((label + "\n").encode())
            await asyncio.Event().wait()

    if phase == "replacement":
        await observe(phase)
    debt = await claim(database.engine, database.Base.metadata)
    assert debt is not None
    if phase == "claimed":
        await observe(phase)
    await run_claimed(database.engine, database.Base.metadata, debt, phase_hook=observe)
    raise AssertionError("selected kill window was not reached")


async def resume():
    await database.init_db()
    from backend.app.models.printer import Printer

    async with database.engine.begin() as conn:
        assert await read_record(conn, DEBT) is None
        indexes = {
            r[0]
            for r in (
                await conn.execute(
                    text(
                        "SELECT indexname FROM pg_indexes WHERE schemaname='public'"
                        if conn.dialect.name == "postgresql"
                        else "SELECT name FROM sqlite_schema WHERE type='index'"
                    )
                )
            ).all()
        }
        assert {r["name"] for r in manifest()} <= indexes
        if conn.dialect.name == "postgresql":
            fks = await conn.run_sync(lambda s: inspect(s).get_foreign_keys("spool_usage_history"))
            fk = next(f for f in fks if f["constrained_columns"] == ["archive_id"])
            assert await conn.scalar(
                text(
                    "SELECT convalidated FROM pg_constraint WHERE conname=:name AND conrelid='spool_usage_history'::regclass"
                ),
                {"name": fk["name"]},
            )
            from sqlalchemy.ext.asyncio import AsyncSession

            from backend.app.models.oidc_provider import OIDCProvider
            from backend.app.models.spool import Spool
            from backend.app.models.spool_usage_history import SpoolUsageHistory

            async with AsyncSession(bind=conn) as session:
                with pytest_integrity("23514"):
                    async with session.begin_nested():
                        session.add(
                            OIDCProvider(
                                name="bad-probe",
                                issuer_url="https://example.invalid",
                                client_id="x",
                                _client_secret_enc="synthetic",
                                auto_link_existing_accounts=True,
                                email_claim="email",
                                require_email_verified=False,
                            )
                        )
                        await session.flush()
                spool = Spool(material="PLA")
                session.add(spool)
                await session.flush()
                with pytest_integrity("23503"):
                    async with session.begin_nested():
                        session.add(SpoolUsageHistory(spool_id=spool.id, archive_id=2147483647, weight_used=1))
                        await session.flush()
    async with database.async_session() as session:
        with pytest_integrity("23505" if database.engine.dialect.name == "postgresql" else None):
            async with session.begin_nested():
                session.add(
                    Printer(
                        name="duplicate",
                        serial_number="D4-CRASH-PROBE",
                        ip_address="127.0.0.1",
                        access_code="synthetic",
                    )
                )
                await session.flush()
    return {
        "debt": None,
        "indexes": len(manifest()),
        "UNIQUE": True,
        "FK": database.engine.dialect.name == "postgresql",
        "CHECK": database.engine.dialect.name == "postgresql",
    }


class pytest_integrity:
    """Require the intended guard's SQLSTATE, not any accidental insert failure."""

    def __init__(self, state):
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        assert isinstance(value, IntegrityError), (kind, value)
        if self.state:
            assert getattr(value.orig, "sqlstate", None) == self.state, value
        return True


async def budget():
    database.import_all_models()
    async with database.engine.begin() as conn:
        debt = await read_record(conn, DEBT)
    return {"budget": debt["automatic_attempts_remaining"]}


def maintenance_client():
    from unittest.mock import AsyncMock, patch

    from sqlalchemy import event
    from starlette.testclient import TestClient

    from backend.app import main as web

    writes = []

    def observe(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().split(None, 1)[0].upper() in {"CREATE", "ALTER", "INSERT", "UPDATE", "DELETE", "DROP"}:
            writes.append(statement.split(None, 1)[0])

    event.listen(database.engine.sync_engine, "before_cursor_execute", observe)
    try:
        with (
            patch(
                "backend.app.core.schema_repair.run_claimed",
                AsyncMock(side_effect=AssertionError("automatic budget bypass")),
            ),
            patch(
                "backend.app.services.camera_runtime.configure_camera_runtime",
                AsyncMock(side_effect=AssertionError("farm startup bypass")),
            ),
            TestClient(web.app) as client,
        ):
            for path in ("/health", "/api/v1/auth/status", "/api/v1/printers", "/"):
                response = client.get(path)
                assert response.status_code == 503
                assert response.json() == {"status": "maintenance", "reason": "schema_repair_pending"}
            assert not web.farm_startup_guard.ready
        assert writes == [], writes
        return {"maintenance": True, "automatic_ddl": False, "farm_started": False}
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", observe)


async def main(mode, phase):
    try:
        if mode == "prepare":
            return await prepare()
        if mode == "prepare-legacy":
            from backend.app.core.config import settings
            from backend.app.core.db_portable import import_sqlite_to_postgres
            from backend.tests.integration.g02_pg_schema_scenarios import unpack

            database.import_all_models()
            path = unpack("v042", Path(settings.data_dir))
            await import_sqlite_to_postgres(database.engine, database.Base.metadata, path)
            async with database.engine.begin() as conn:
                assert await conn.scalar(text("SELECT count(*) FROM printers")) == 0
                debt = await read_record(conn, DEBT)
            return {"budget": debt["automatic_attempts_remaining"]}
        if mode == "resume-legacy":
            from backend.app.core.config import settings
            from backend.app.models.settings import Settings

            source = Path(settings.data_dir) / "bamdude.db"
            before = source.read_bytes()
            await database.init_db()
            async with database.async_session() as session:
                session.add(Settings(key="d4_target_only", value="keep after repair"))
                await session.commit()
            # Still no printers: completed repair history must prevent another
            # local-file auto-import from erasing the target-only setting.
            await database.init_db()
            async with database.engine.begin() as conn:
                assert await read_record(conn, DEBT) is None
                assert (
                    await conn.scalar(text("SELECT value FROM settings WHERE \"key\"='d4_target_only'"))
                    == "keep after repair"
                )
                assert await conn.scalar(text("SELECT max(version) FROM _migrations")) >= 230
            assert source.read_bytes() == before
            assert not source.with_name(source.name + ".migrated").exists()
            return {"legacy_resumed": True, "source_not_consumed": True, "target_setting_retained": True, "debt": None}
        if mode == "kill":
            return await kill_window(phase)
        if mode == "resume":
            return await resume()
        if mode == "budget":
            return await budget()
        if mode == "maintenance":
            return maintenance_client()
        if mode == "exhaust":
            database.import_all_models()
            async with database.engine.begin() as conn:
                for _ in range(2):
                    await claim_attempt(conn, database.Base.metadata)
            return {"budget": 0}
        raise AssertionError(mode)
    finally:
        await database.engine.dispose()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""))))
