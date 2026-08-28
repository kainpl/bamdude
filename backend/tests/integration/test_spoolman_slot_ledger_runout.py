"""Syncing the AMS must not unlink a spool that merely ran out.

``spoolman_slot_assignments`` is how a **tag-less** spool — one assigned through
the BamDude UI rather than read off an RFID chip — is resolved at print
completion (see ``test_spoolman_tracking_slot_fallback``). The sync endpoints
maintain that ledger from what the AMS reports, deleting the row for any slot
that comes back empty.

⚠️ A slot that reports empty **during a print** is a filament runout, not a
spool somebody took out: the spool is still in the bay, just consumed. Deleting
the row there loses the runout segment's usage — and with AMS filament backup
that is exactly the segment the substitute spool would otherwise be charged for
(upstream `454457a0`, adapted: upstream guards this in their AMS callback, which
maintains the ledger on every MQTT push; ours is maintained only by these
endpoints, so the guard belongs here).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

# One AMS with one slot, and the slot reports nothing at all.
EMPTY_SLOT_PUSH = [{"id": 0, "tray": [{"id": 0}]}]


@pytest.fixture
async def spoolman_enabled(db_session: AsyncSession):
    from backend.app.models.settings import Settings

    db_session.add(Settings(key="spoolman_enabled", value="true"))
    db_session.add(Settings(key="spoolman_url", value="http://localhost:7912"))
    await db_session.commit()


@pytest.fixture
def spoolman_client():
    client = MagicMock()
    client.is_connected = True
    client.base_url = "http://localhost:7912"
    client.health_check = AsyncMock(return_value=True)
    client.get_spools = AsyncMock(return_value=[])
    # An empty tray parses to nothing — the branch the ledger delete hangs off.
    client.parse_ams_tray = MagicMock(return_value=None)
    with patch(
        "backend.app.api.routes.spoolman.get_spoolman_client",
        AsyncMock(return_value=client),
    ):
        yield client


def _state(printer_state: str):
    state = MagicMock()
    state.raw_data = {"ams": EMPTY_SLOT_PUSH}
    state.state = printer_state
    return state


async def _ledger_rows(db_session: AsyncSession, printer_id: int):
    result = await db_session.execute(
        select(SpoolmanSlotAssignment).where(SpoolmanSlotAssignment.printer_id == printer_id)
    )
    return result.scalars().all()


async def _assigned_printer(printer_factory, db_session: AsyncSession):
    printer = await printer_factory(name="X1C")
    db_session.add(SpoolmanSlotAssignment(printer_id=printer.id, ams_id=0, tray_id=0, spoolman_spool_id=42))
    await db_session.commit()
    return printer


class TestSyncingOnePrinter:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_ledger_row_survives_a_runout(
        self, async_client: AsyncClient, printer_factory, db_session: AsyncSession, spoolman_enabled, spoolman_client
    ):
        printer = await _assigned_printer(printer_factory, db_session)

        with patch("backend.app.api.routes.spoolman.printer_manager") as pm:
            pm.get_status.return_value = _state("RUNNING")
            response = await async_client.post(f"/api/v1/spoolman/sync/{printer.id}")

        assert response.status_code == 200
        assert len(await _ledger_rows(db_session, printer.id)) == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_an_idle_printer_still_clears_the_row(
        self, async_client: AsyncClient, printer_factory, db_session: AsyncSession, spoolman_enabled, spoolman_client
    ):
        """The guard defers the cleanup, it does not cancel it."""
        printer = await _assigned_printer(printer_factory, db_session)

        with patch("backend.app.api.routes.spoolman.printer_manager") as pm:
            pm.get_status.return_value = _state("IDLE")
            response = await async_client.post(f"/api/v1/spoolman/sync/{printer.id}")

        assert response.status_code == 200
        assert await _ledger_rows(db_session, printer.id) == []


class TestSyncingEveryPrinter:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_ledger_row_survives_a_runout(
        self, async_client: AsyncClient, printer_factory, db_session: AsyncSession, spoolman_enabled, spoolman_client
    ):
        """Sync-all reads the same push for every printer and needs the same guard —
        a farm is the case where somebody presses Sync while something is printing."""
        printer = await _assigned_printer(printer_factory, db_session)

        with patch("backend.app.api.routes.spoolman.printer_manager") as pm:
            pm.get_status.return_value = _state("RUNNING")
            response = await async_client.post("/api/v1/spoolman/sync-all")

        assert response.status_code == 200
        assert len(await _ledger_rows(db_session, printer.id)) == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_an_idle_printer_still_clears_the_row(
        self, async_client: AsyncClient, printer_factory, db_session: AsyncSession, spoolman_enabled, spoolman_client
    ):
        printer = await _assigned_printer(printer_factory, db_session)

        with patch("backend.app.api.routes.spoolman.printer_manager") as pm:
            pm.get_status.return_value = _state("IDLE")
            response = await async_client.post("/api/v1/spoolman/sync-all")

        assert response.status_code == 200
        assert await _ledger_rows(db_session, printer.id) == []
