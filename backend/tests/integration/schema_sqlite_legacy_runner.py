"""Real legacy SQLite, actual HTTP restore/CLI, no farm managers or live paths."""

import asyncio
import io
import json
import sys
import zipfile
from pathlib import Path

import httpx
from fastapi import FastAPI
from sqlalchemy import text


async def main(mode, fixture):
    from backend.app.api.routes.settings import router
    from backend.app.core import database, schema_repair
    from backend.app.core.auth_runtime import auth_runtime
    from backend.app.core.config import settings
    from backend.app.core.schema_repair_state import DEBT, RESIDUALS, claim_attempt, read_record, write_pending
    from backend.app.models.settings import Settings
    from backend.app.models.user import User
    from backend.app.services.connector_submissions import server_identity
    from backend.app.services.farm_startup_guard import farm_startup_guard
    from backend.tests.auth_helpers import mint_test_access
    from backend.tests.integration.g02_pg_schema_scenarios import unpack

    database.import_all_models()
    try:
        if mode in ("control", "debt", "prepare-cli", "prepare-partial-cli"):
            unpack(fixture, Path(settings.data_dir))
            if mode != "control":
                async with database.engine.begin() as conn:
                    await write_pending(conn, database.Base.metadata)
                    if mode == "prepare-cli":
                        await claim_attempt(conn, database.Base.metadata)
                        await claim_attempt(conn, database.Base.metadata)
            if mode == "prepare-cli":
                return {"budget": 0}
            if mode == "prepare-partial-cli":
                from unittest.mock import Mock, patch

                from backend.app.core.schema_repair_state import SchemaRepairPending

                # Reproduce the actual pre-fix failure, rather than inventing
                # a marker: real v040 executes its chain without create_all
                # until m099 fails, then the fixed offline CLI must resume it.
                with patch.object(database.Base.metadata, "create_all", Mock()):
                    try:
                        await database.init_db()
                    except SchemaRepairPending as exc:
                        assert exc.reason == "migration_failure"
                    else:
                        raise AssertionError("old SQLite order no longer reproduced")
                async with database.engine.begin() as conn:
                    assert await conn.scalar(text("SELECT max(version) FROM _migrations")) == 98
                    assert (await read_record(conn, DEBT))["automatic_attempts_remaining"] == 0
                return {"budget": 0}
        if mode == "http":
            await database.init_db()
            farm_startup_guard.start(Path(settings.data_dir))
            auth_runtime.open_admission()
            async with database.async_session() as db:
                admin = User(username="legacy-admin", role="admin", auth_source="local", is_active=True)
                db.add_all([admin, Settings(key="pre_restore_target", value="must disappear")])
                await db.commit()
                token = await mint_test_access(db, user=admin)
                await server_identity(db)
            source = unpack(fixture, Path(settings.data_dir).parent / "old-source")
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as archive:
                archive.write(source, "bamdude.db")
            app = FastAPI()
            app.include_router(router, prefix="/api/v1")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/settings/restore",
                    headers={"Authorization": "Bearer " + token},
                    files={"file": ("legacy.zip", stream.getvalue(), "application/zip")},
                )
            assert response.status_code == 200 and response.json()["success"], response.text
            async with database.engine.begin() as conn:
                assert await conn.scalar(text("SELECT count(*) FROM settings WHERE key='pre_restore_target'")) == 0
        else:
            await database.init_db()
        if mode == "residual-failure":
            from unittest.mock import AsyncMock, patch

            async with database.engine.begin() as conn:
                before = await read_record(conn, RESIDUALS)
                assert await read_record(conn, DEBT) is None
            with patch.object(
                schema_repair, "retry_index_residuals", AsyncMock(side_effect=OSError("DO NOT LOG THIS VALUE"))
            ):
                await database.init_db()  # An optional retry cannot fail native startup.
            async with database.engine.begin() as conn:
                assert await read_record(conn, DEBT) is None
                assert await read_record(conn, RESIDUALS) == before
            return {"startup_completed": True, "new_hard_debt": False}
        async with database.engine.begin() as conn:
            assert await read_record(conn, DEBT) is None
            level = await conn.scalar(text("SELECT max(version) FROM _migrations"))
            rows = (
                await conn.execute(
                    text(
                        "SELECT type,name,tbl_name,sql FROM sqlite_schema "
                        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
                    )
                )
            ).all()
            catalogue = [list(row) for row in rows]
        await database.init_db()  # Ordinary restart after debt completion.
        async with database.engine.begin() as conn:
            assert await read_record(conn, DEBT) is None
        return {"level": level, "catalogue": catalogue, "debt": None}
    finally:
        farm_startup_guard.finish(clean=False)
        await database.engine.dispose()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "v040"))))
