"""The Spoolman slot-assignment endpoint reports a replace and broadcasts it.

``POST /spoolman/inventory/slot-assignments`` has always been an upsert — the
slot's row is updated in place when it already holds another spool. Spec
2026-09-13 §3.3 makes that visible: the response carries
``replaced_spoolman_spool_id`` (``None`` when nothing was displaced) and the
route broadcasts ``spool_assignment_changed``, the event the internal assign
and both unassigns already send and this one never did — which is why a
Spoolman card only refreshed on the next poll.

Nothing about the replace semantics changes here: an occupied slot is never
refused, and the usage journal is told exactly as before.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

AMS_ID = 0
TRAY_ID = 1


def _spoolman_spool(spool_id: int) -> dict:
    """Minimal raw Spoolman spool that ``_map_spoolman_spool`` accepts."""
    return {
        "id": spool_id,
        "remaining_weight": 800.0,
        "used_weight": 200.0,
        "registered": "2026-09-01T10:00:00",
        "filament": {
            "id": 5,
            "name": "PLA Basic",
            "material": "PLA",
            "color_hex": "FF0000",
            "weight": 1000,
            "spool_weight": 250,
            "settings_extruder_temp": 220,
            "vendor": {"id": 1, "name": "Bambu Lab"},
        },
        "extra": {},
    }


def _mock_spoolman_client(spool: dict) -> MagicMock:
    client = MagicMock()
    client.get_spool = AsyncMock(return_value=spool)
    # #1457 stale-fallback-tag cleanup enumerates Spoolman's spools; nothing
    # in these tests holds this slot's fallback tag.
    client.get_spools = AsyncMock(return_value=[])
    client.merge_spool_extra = AsyncMock(return_value=None)
    return client


async def _seed_row(db_session: AsyncSession, printer_id: int, spoolman_spool_id: int) -> None:
    db_session.add(
        SpoolmanSlotAssignment(
            printer_id=printer_id,
            ams_id=AMS_ID,
            tray_id=TRAY_ID,
            spoolman_spool_id=spoolman_spool_id,
        )
    )
    await db_session.commit()


async def _slot_spool_ids(db_session: AsyncSession, printer_id: int) -> list[int]:
    """The slot's assigned Spoolman spool ids, straight from the database — a
    column-only select, because the upsert changes the row in place and the
    identity map would otherwise hand back the pre-assign value."""
    result = await db_session.execute(
        select(SpoolmanSlotAssignment.spoolman_spool_id).where(
            SpoolmanSlotAssignment.printer_id == printer_id,
            SpoolmanSlotAssignment.ams_id == AMS_ID,
            SpoolmanSlotAssignment.tray_id == TRAY_ID,
        )
    )
    return list(result.scalars().all())


async def _assign(async_client: AsyncClient, printer_id: int, spoolman_spool_id: int):
    """POST the assignment with the Spoolman client and MQTT layer stubbed;
    returns (response, broadcast spy)."""
    client = _mock_spoolman_client(_spoolman_spool(spoolman_spool_id))
    with (
        patch(
            "backend.app.api.routes.spoolman_inventory._get_client",
            new=AsyncMock(return_value=client),
        ),
        patch("backend.app.api.routes.spoolman_inventory.printer_manager") as mock_pm,
        patch("backend.app.core.websocket.ws_manager.broadcast", new_callable=AsyncMock) as broadcast,
    ):
        # No live MQTT client → the best-effort auto-configure block is skipped.
        mock_pm.get_client.return_value = None

        response = await async_client.post(
            "/api/v1/spoolman/inventory/slot-assignments",
            json={
                "spoolman_spool_id": spoolman_spool_id,
                "printer_id": printer_id,
                "ams_id": AMS_ID,
                "tray_id": TRAY_ID,
            },
        )
    return response, broadcast


def _assert_slot_change_broadcast(broadcast, printer_id: int) -> None:
    payloads = [call.args[0] for call in broadcast.await_args_list if call.args]
    assert {
        "type": "spool_assignment_changed",
        "printer_id": printer_id,
        "ams_id": AMS_ID,
        "tray_id": TRAY_ID,
    } in payloads


class TestSpoolmanAssignSaysWhatItReplaced:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_assigning_over_an_occupied_slot_replaces_and_says_so(
        self, async_client: AsyncClient, printer_factory, db_session: AsyncSession, caplog
    ):
        printer = await printer_factory(name="X1C")
        await _seed_row(db_session, printer.id, 11)

        caplog.set_level(logging.INFO, logger="backend.app.api.routes.spoolman_inventory")
        response, broadcast = await _assign(async_client, printer.id, 12)

        assert response.status_code == 200
        body = response.json()
        assert body["replaced_spoolman_spool_id"] == 11
        assert body["id"] == 12

        assert await _slot_spool_ids(db_session, printer.id) == [12]
        _assert_slot_change_broadcast(broadcast, printer.id)
        assert f"Slot {printer.id}/{AMS_ID}/{TRAY_ID}: spool 11 replaced with 12" in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_first_assignment_reports_no_replacement_and_still_broadcasts(
        self, async_client: AsyncClient, printer_factory, db_session: AsyncSession
    ):
        printer = await printer_factory(name="X1C")

        response, broadcast = await _assign(async_client, printer.id, 12)

        assert response.status_code == 200
        assert response.json()["replaced_spoolman_spool_id"] is None

        assert await _slot_spool_ids(db_session, printer.id) == [12]
        _assert_slot_change_broadcast(broadcast, printer.id)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_reassigning_the_same_spool_reports_no_replacement(
        self, async_client: AsyncClient, printer_factory, db_session: AsyncSession
    ):
        """The upsert is idempotent for the spool already on the slot, so
        nothing was displaced and the field stays None."""
        printer = await printer_factory(name="X1C")
        await _seed_row(db_session, printer.id, 12)

        response, _broadcast = await _assign(async_client, printer.id, 12)

        assert response.status_code == 200
        assert response.json()["replaced_spoolman_spool_id"] is None
        assert await _slot_spool_ids(db_session, printer.id) == [12]
