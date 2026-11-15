"""Actual signed-admin HTTP restore of a backup with the X2 data residual."""

import asyncio
import json
from pathlib import Path

import httpx
from fastapi import FastAPI
from sqlalchemy import text


async def main():
    from backend.app.api.routes.settings import create_backup_zip, router
    from backend.app.core import database
    from backend.app.core.auth_runtime import auth_runtime
    from backend.app.core.config import settings
    from backend.app.core.schema_index_repair import native_upgrade, retry_index_residuals
    from backend.app.core.schema_repair_state import AUDIT, DEBT, RESIDUALS, read_record
    from backend.app.models.filament_calibration import FilamentCalibration
    from backend.app.models.printer import Printer
    from backend.app.models.user import User
    from backend.app.services.connector_submissions import server_identity
    from backend.app.services.farm_startup_guard import farm_startup_guard
    from backend.tests.auth_helpers import mint_test_access

    await database.init_db()
    farm_startup_guard.start(Path(settings.data_dir))
    auth_runtime.open_admission()
    try:
        async with database.async_session() as db:
            user = User(username="d4-consent-admin", role="admin", auth_source="local", is_active=True)
            printer = Printer(
                name="consent-probe", serial_number="D4-CONSENT", ip_address="127.0.0.1", access_code="synthetic"
            )
            db.add_all([user, printer])
            await db.flush()
            printer_id = printer.id
            await db.commit()
            token = await mint_test_access(db, user=user)
            await server_identity(db)
        async with database.engine.begin() as conn:
            await conn.execute(text("DROP INDEX ux_filament_cali_active"))
        async with database.async_session() as db:
            for label in ("first", "second"):
                db.add(
                    FilamentCalibration(
                        printer_id=printer_id,
                        filament_id="synthetic-filament",
                        nozzle_diameter=0.4,
                        nozzle_volume_type="standard",
                        extruder_id=0,
                        cali_mode="pa",
                        source="manual",
                        is_active=True,
                        name=label,
                        notes="preserve " + label,
                    )
                )
            await db.commit()
        async with database.engine.begin() as conn:
            await native_upgrade(conn)
            before = [
                tuple(r) for r in (await conn.execute(text("SELECT * FROM filament_calibration ORDER BY id"))).all()
            ]
        path, _ = await create_backup_zip(Path(settings.data_dir))
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            data = path.read_bytes()
            first = await client.post(
                "/api/v1/settings/restore",
                headers={"Authorization": "Bearer " + token},
                files={"file": ("same.zip", data, "application/zip")},
            )
            assert first.status_code == 400, first.text
            detail = first.json()["detail"]
            assert detail["error"] == "calibration_restore_confirmation_required" and detail["duplicate_groups"] == 1
            assert isinstance(detail["receipt"], str)
            assert auth_runtime.admitting and not farm_startup_guard.restart_required
            second = await client.post(
                "/api/v1/settings/restore",
                headers={"Authorization": "Bearer " + token},
                files={"file": ("same.zip", data, "application/zip")},
                data={"accept_calibration_residual": "true", "calibration_receipt": detail["receipt"]},
            )
            assert second.status_code == 200 and second.json().get("success") is True, second.text
        async with database.engine.begin() as conn:
            after = [
                tuple(r) for r in (await conn.execute(text("SELECT * FROM filament_calibration ORDER BY id"))).all()
            ]
            assert before == after
            assert await read_record(conn, DEBT) is None
            record = await read_record(conn, RESIDUALS)
            item = record["entries"]["filament_calibration.indexes.ux_filament_cali_active"]
            assert item["count"] == 1 and item["provenance"] == "accepted-restore"
            audit = await read_record(conn, item["acceptance"])
            assert item["acceptance"].startswith(AUDIT)
            assert audit["duplicate_groups"] == 1 and audit["actor"]["account_identity"] == user.account_identity
            assert audit["accepted_object"] == item["object"]
            await conn.execute(text("UPDATE filament_calibration SET is_active=FALSE WHERE name='second'"))
            await retry_index_residuals(conn)
            assert (await read_record(conn, RESIDUALS))["entries"] == {}
        return {
            "refusal_before_drain": True,
            "confirmed_http_restore": True,
            "same_rows_and_flags": True,
            "durable_audit": True,
            "targeted_repair": True,
            "dialect": database.engine.dialect.name,
        }
    finally:
        farm_startup_guard.finish(clean=False)
        await database.engine.dispose()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main())))
