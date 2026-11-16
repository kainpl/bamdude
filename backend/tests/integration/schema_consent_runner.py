"""Actual signed-admin HTTP restore of a backup with the X2 data residual."""

import asyncio
import json
import sys
from contextlib import ExitStack
from pathlib import Path

import httpx
from fastapi import FastAPI
from sqlalchemy import text


async def main(mode="normal"):
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
            from unittest.mock import patch

            from backend.app.core import schema_repair
            from backend.app.services.backup_files import FileRestore
            from backend.app.services.virtual_printer import virtual_printer_manager

            prepared = []
            real_prepare = FileRestore.prepare
            real_drain = virtual_printer_manager.drain_for_restore
            real_repair = schema_repair.run_claimed

            def observe_prepare(owner):
                prepared.append(True)
                return real_prepare(owner)

            async def change_target():
                from backend.app.services.connector_submissions import rotate_storage_epoch

                result = await real_drain()
                async with database.async_session() as db:
                    await rotate_storage_epoch(db)
                return result

            async def change_count(engine, metadata, debt, **kwargs):
                async def after_indexes(label):
                    if label == "D4-indexes":
                        async with database.async_session() as db:
                            for name in ("extra-first", "extra-second"):
                                db.add(
                                    FilamentCalibration(
                                        printer_id=printer_id,
                                        filament_id="post-conversion-group",
                                        nozzle_diameter=0.4,
                                        nozzle_volume_type="standard",
                                        extruder_id=0,
                                        cali_mode="pa",
                                        source="manual",
                                        is_active=True,
                                        name=name,
                                    )
                                )
                            await db.commit()

                return await real_repair(engine, metadata, debt, phase_hook=after_indexes)

            with ExitStack() as patches:
                patches.enter_context(patch.object(FileRestore, "prepare", observe_prepare))
                if mode == "target-shift":
                    patches.enter_context(patch.object(virtual_printer_manager, "drain_for_restore", change_target))
                elif mode == "count-shift":
                    patches.enter_context(patch.object(schema_repair, "run_claimed", change_count))
                second = await client.post(
                    "/api/v1/settings/restore",
                    headers={"Authorization": "Bearer " + token},
                    files={"file": ("same.zip", data, "application/zip")},
                    data={"accept_calibration_residual": "true", "calibration_receipt": detail["receipt"]},
                )
            if mode == "target-shift":
                assert second.status_code == 400, second.text
                assert "no longer matches" in second.text
                assert prepared == []
                async with database.engine.begin() as conn:
                    assert await read_record(conn, DEBT) is None
                    assert before == [
                        tuple(r)
                        for r in (await conn.execute(text("SELECT * FROM filament_calibration ORDER BY id"))).all()
                    ]
                return {"target_rechecked_after_drain": True, "replacement_started": False}
            if mode == "count-shift":
                assert second.status_code == 503, second.text
                async with database.engine.begin() as conn:
                    debt = await read_record(conn, DEBT)
                    assert debt["last_failure"] == "acceptance_residual_changed"
                    assert debt["automatic_attempts_remaining"] == 0
                    assert await conn.scalar(text("SELECT count(*) FROM filament_calibration")) == 4
                return {"changed_count_refused": True, "debt_retained": True}
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
    print(json.dumps(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "normal"))))
