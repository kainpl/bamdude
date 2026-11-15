"""No built-in slot assignment is written while Spoolman mode is on.

The switch to Spoolman deletes every ``spool_assignments`` row, so a built-in
row exists in Spoolman mode only if something WRITES one — and the assign route
did, whenever the card fell back to the built-in dialog because Spoolman was
enabled but unreachable (upstream 62fad59fd, issue #3430). Such a row was then
never cleared and reached the card, the Inventory page, the print dialog, the
journal's restart seed and the missing-assignment warning. The guard belongs at
the write; an install that already holds stray rows loses them at start-up.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import ams_advertised_overlay as overlay


async def _spool(db: AsyncSession) -> Spool:
    spool = Spool(material="PLA", subtype="Basic", brand="Generic", rgba="FF0000FF", label_weight=1000, weight_used=0)
    db.add(spool)
    await db.commit()
    await db.refresh(spool)
    return spool


async def _spoolman_mode(db: AsyncSession, on: bool = True) -> None:
    db.add(Settings(key="spoolman_enabled", value="true" if on else "false"))
    await db.commit()


async def _row_count(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(SpoolAssignment)) or 0)


@pytest.mark.asyncio
@pytest.mark.integration
class TestTheWriteIsRefused:
    async def test_assign_is_409_and_writes_publishes_and_remembers_nothing(
        self, async_client: AsyncClient, printer_factory, db_session: AsyncSession
    ):
        printer = await printer_factory(name="X1C")
        spool = await _spool(db_session)
        await _spoolman_mode(db_session)

        with patch("backend.app.services.printer_manager.printer_manager") as pm:
            mqtt = MagicMock()
            pm.get_client.return_value = mqtt
            pm.get_status.return_value = None
            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 0, "tray_id": 1},
            )

        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "spoolman_mode"
        assert await _row_count(db_session) == 0
        mqtt.ams_set_filament_setting.assert_not_called()
        assert overlay.entries_for(printer.id) == {}

    async def test_unassign_is_409_too(self, async_client: AsyncClient, printer_factory, db_session: AsyncSession):
        printer = await printer_factory(name="X1C")
        await _spoolman_mode(db_session)

        response = await async_client.delete(f"/api/v1/inventory/assignments/{printer.id}/0/1")

        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "spoolman_mode"

    async def test_add_from_slot_is_409_and_creates_no_built_in_spool(
        self, async_client: AsyncClient, printer_factory, db_session: AsyncSession
    ):
        """The third writer: "+ Add to inventory" made a built-in spool AND its
        slot row whenever the page had not learnt the mode (review slots I-4)."""
        printer = await printer_factory(name="X1C")
        await _spoolman_mode(db_session)
        state = MagicMock()
        state.raw_data = {
            "ams": [{"id": 0, "tray": [{"id": 1, "tray_type": "PLA", "tray_color": "FF0000FF", "tag_uid": "A1B2C3D4"}]}]
        }

        with patch("backend.app.services.printer_manager.printer_manager") as pm:
            pm.get_status.return_value = state
            response = await async_client.post(
                "/api/v1/inventory/spools/from-slot", json={"printer_id": printer.id, "ams_id": 0, "tray_id": 1}
            )

        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "spoolman_mode"
        assert await _row_count(db_session) == 0
        assert int(await db_session.scalar(select(func.count()).select_from(Spool)) or 0) == 0

    async def test_the_built_in_mode_still_assigns(
        self, async_client: AsyncClient, printer_factory, db_session: AsyncSession
    ):
        printer = await printer_factory(name="X1C")
        spool = await _spool(db_session)
        await _spoolman_mode(db_session, on=False)

        with patch("backend.app.services.printer_manager.printer_manager") as pm:
            pm.ensure_fresh_connection = AsyncMock(return_value=True)
            pm.ensure_fresh_connection_for_printer = AsyncMock(return_value=True)
            pm.get_client.return_value = None
            status = MagicMock()
            status.raw_data = {}
            pm.get_status.return_value = status
            response = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 0, "tray_id": 1},
            )

        assert response.status_code == 200
        assert await _row_count(db_session) == 1


@pytest.mark.asyncio
@pytest.mark.integration
class TestStrayRowsGoAtStartUp:
    async def test_rows_left_by_the_hole_are_removed_in_spoolman_mode(self, printer_factory, db_session: AsyncSession):
        from backend.app.api.routes.settings import clear_stray_builtin_assignments

        printer = await printer_factory(name="X1C")
        spool = await _spool(db_session)
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=0, tray_id=1))
        await _spoolman_mode(db_session)

        assert await clear_stray_builtin_assignments(db_session) == 1
        assert await _row_count(db_session) == 0

    async def test_what_was_advertised_for_those_rows_is_forgotten_with_them(
        self, printer_factory, db_session: AsyncSession
    ):
        """An overlay entry for a row that no longer exists would go on masking
        the slot — the mode switch forgets them for the same reason (review slots M-2)."""
        from backend.app.api.routes.settings import clear_stray_builtin_assignments
        from backend.app.services.ams_advertised_overlay import OverlayEntry

        printer = await printer_factory(name="X1C")
        spool = await _spool(db_session)
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=0, tray_id=1))
        await _spoolman_mode(db_session)
        overlay.replace_printer(
            printer.id, {(0, 1): OverlayEntry("PLA", "FF0000FF", "GFL99", (), "000000FF", "GFL99", "internal")}
        )

        await clear_stray_builtin_assignments(db_session)

        assert overlay.entries_for(printer.id) == {}

    def test_start_up_removes_them_before_any_printer_connects(self):
        """After the printers connect, a first AMS push could rebuild the
        overlay from rows the clean-up was about to delete."""
        from pathlib import Path

        import backend.app.main as main

        source = Path(main.__file__).read_text(encoding="utf-8")
        assert source.count("await clear_stray_builtin_assignments(db)") == 1
        assert source.index("await clear_stray_builtin_assignments(db)") < source.index(
            "await init_printer_connections(db)"
        )

    async def test_the_built_in_modes_rows_are_left_alone(self, printer_factory, db_session: AsyncSession):
        from backend.app.api.routes.settings import clear_stray_builtin_assignments

        printer = await printer_factory(name="X1C")
        spool = await _spool(db_session)
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=0, tray_id=1))
        await db_session.commit()

        assert await clear_stray_builtin_assignments(db_session) == 0
        assert await _row_count(db_session) == 1
