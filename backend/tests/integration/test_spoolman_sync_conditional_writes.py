"""A Spoolman AMS sync writes only what it changed, where the slot still holds what it read.

Both Sync buttons read ``spoolman_slot_assignments``, then ask Spoolman once per
tray, then write. A hand assignment or unassignment that commits while those
requests are in flight used to be overwritten — silently, because the
announcement compared with the ledger the sync had read (upstream fcc20cb62).

The tests act between the read and the write: the stubbed ``sync_ams_tray`` (or
``parse_ams_tray`` for an empty tray) changes the ledger the way the assign
routes do, then answers as Spoolman would have.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

EMPTY_SLOT_PUSH = [{"id": 0, "tray": [{"id": 0}]}]
LOADED_SLOT_PUSH = [{"id": 0, "tray": [{"id": 0, "tray_type": "PLA"}]}]
ENDPOINTS = ["/api/v1/spoolman/sync/{id}", "/api/v1/spoolman/sync-all"]


@pytest.fixture
async def spoolman_enabled(db_session: AsyncSession):
    from backend.app.models.settings import Settings

    db_session.add(Settings(key="spoolman_enabled", value="true"))
    db_session.add(Settings(key="spoolman_url", value="http://localhost:7912"))
    await db_session.commit()


def _tray(tag: str = ""):
    return SimpleNamespace(
        tray_id=0,
        tray_uuid=tag,
        tag_uid="",
        tray_type="PLA",
        tray_color="FF0000FF",
        tray_sub_brands="PLA Basic",
    )


def _state(ams: list):
    state = MagicMock()
    state.raw_data = {"ams": ams}
    state.state = "IDLE"
    return state


async def _hand_assign(db: AsyncSession, printer_id: int, spool_id: int) -> None:
    await db.execute(
        text(
            "INSERT INTO spoolman_slot_assignments (printer_id, ams_id, tray_id, spoolman_spool_id)"
            " VALUES (:p, 0, 0, :s)"
            " ON CONFLICT(printer_id, ams_id, tray_id) DO UPDATE SET spoolman_spool_id = excluded.spoolman_spool_id"
        ),
        {"p": printer_id, "s": spool_id},
    )
    await db.commit()


async def _hand_unassign(db: AsyncSession, printer_id: int) -> None:
    await db.execute(delete(SpoolmanSlotAssignment).where(SpoolmanSlotAssignment.printer_id == printer_id))
    await db.commit()


async def _slot(db: AsyncSession, printer_id: int) -> list[int]:
    return list(
        (
            await db.execute(
                select(SpoolmanSlotAssignment.spoolman_spool_id).where(SpoolmanSlotAssignment.printer_id == printer_id)
            )
        )
        .scalars()
        .all()
    )


def _announced(broadcast: AsyncMock) -> list[tuple[int, int, int]]:
    return [
        (m["printer_id"], m["ams_id"], m["tray_id"])
        for m in (call.args[0] for call in broadcast.await_args_list)
        if m.get("type") == "spool_assignment_changed"
    ]


async def _sync(async_client: AsyncClient, path: str, *, ams: list, tray=None, during=None, answers: int | None = None):
    """Run a sync; ``during`` runs between the ledger read and the write."""
    client = MagicMock()
    client.is_connected = True
    client.base_url = "http://localhost:7912"
    client.health_check = AsyncMock(return_value=True)
    client.get_spools = AsyncMock(return_value=[])

    async def sync_ams_tray(*_args, **kwargs):
        if during:
            await during()
        spool_id = answers if answers is not None else kwargs.get("spoolman_spool_id_hint")
        return {"id": spool_id} if spool_id else None

    client.sync_ams_tray = AsyncMock(side_effect=sync_ams_tray)
    client.parse_ams_tray = MagicMock(return_value=tray)

    with (
        patch("backend.app.api.routes.spoolman.get_spoolman_client", AsyncMock(return_value=client)),
        patch("backend.app.api.routes.spoolman.printer_manager") as pm,
        patch("backend.app.core.websocket.ws_manager.broadcast", new_callable=AsyncMock) as broadcast,
    ):
        pm.get_status.return_value = _state(ams)
        response = await async_client.post(path)
    assert response.status_code == 200
    return response.json(), broadcast


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("endpoint", ENDPOINTS)
class TestAHandChangeMadeDuringTheSyncSurvives:
    async def test_a_hand_assignment_is_not_put_back_to_the_old_spool(
        self, endpoint, async_client: AsyncClient, printer_factory, db_session: AsyncSession, spoolman_enabled
    ):
        """Tag-less tray: the sync's hint is the OLD spool, and Spoolman answers it."""
        printer = await printer_factory(name="X1C")
        await _hand_assign(db_session, printer.id, 42)

        async def operator_assigns():
            await _hand_assign(db_session, printer.id, 99)

        _, broadcast = await _sync(
            async_client, endpoint.format(id=printer.id), ams=LOADED_SLOT_PUSH, tray=_tray(), during=operator_assigns
        )

        assert await _slot(db_session, printer.id) == [99]
        assert _announced(broadcast) == []

    async def test_a_hand_assignment_survives_a_tagged_spool_the_sync_resolved(
        self, endpoint, async_client: AsyncClient, printer_factory, db_session: AsyncSession, spoolman_enabled
    ):
        """The sync found another spool by tag — but the slot no longer holds what it read."""
        printer = await printer_factory(name="X1C")
        await _hand_assign(db_session, printer.id, 42)

        async def operator_assigns():
            await _hand_assign(db_session, printer.id, 99)

        _, broadcast = await _sync(
            async_client,
            endpoint.format(id=printer.id),
            ams=LOADED_SLOT_PUSH,
            tray=_tray(tag="A" * 32),
            during=operator_assigns,
            answers=77,
        )

        assert await _slot(db_session, printer.id) == [99]
        assert _announced(broadcast) == []

    async def test_a_hand_unassignment_stays_unassigned(
        self, endpoint, async_client: AsyncClient, printer_factory, db_session: AsyncSession, spoolman_enabled
    ):
        printer = await printer_factory(name="X1C")
        await _hand_assign(db_session, printer.id, 42)

        async def operator_unassigns():
            await _hand_unassign(db_session, printer.id)

        _, broadcast = await _sync(
            async_client, endpoint.format(id=printer.id), ams=LOADED_SLOT_PUSH, tray=_tray(), during=operator_unassigns
        )

        assert await _slot(db_session, printer.id) == []
        assert _announced(broadcast) == []

    async def test_a_slot_read_empty_and_assigned_meanwhile_is_not_deleted(
        self, endpoint, async_client: AsyncClient, printer_factory, db_session: AsyncSession, spoolman_enabled
    ):
        """Slot 0 is empty and had no row when read; while the sync asks Spoolman
        about slot 1, the operator pre-assigns slot 0."""
        printer = await printer_factory(name="X1C")
        two_slots = [{"id": 0, "tray": [{"id": 0}, {"id": 1, "tray_type": "PLA"}]}]
        loaded = _tray()
        loaded.tray_id = 1

        async def operator_assigns():
            await _hand_assign(db_session, printer.id, 99)

        client = MagicMock()
        client.is_connected = True
        client.base_url = "http://localhost:7912"
        client.health_check = AsyncMock(return_value=True)
        client.get_spools = AsyncMock(return_value=[])
        client.parse_ams_tray = MagicMock(side_effect=lambda _ams, data: loaded if data.get("tray_type") else None)

        async def sync_ams_tray(*_a, **_k):
            await operator_assigns()
            return None

        client.sync_ams_tray = AsyncMock(side_effect=sync_ams_tray)
        with (
            patch("backend.app.api.routes.spoolman.get_spoolman_client", AsyncMock(return_value=client)),
            patch("backend.app.api.routes.spoolman.printer_manager") as pm,
            patch("backend.app.core.websocket.ws_manager.broadcast", new_callable=AsyncMock) as broadcast,
        ):
            pm.get_status.return_value = _state(two_slots)
            response = await async_client.post(endpoint.format(id=printer.id))

        assert response.status_code == 200
        assert await _slot(db_session, printer.id) == [99]
        assert _announced(broadcast) == []


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("endpoint", ENDPOINTS)
class TestWhatTheSyncItselfChangedIsStillWritten:
    async def test_a_genuine_swap_is_written_and_announced(
        self, endpoint, async_client: AsyncClient, printer_factory, db_session: AsyncSession, spoolman_enabled
    ):
        printer = await printer_factory(name="X1C")
        await _hand_assign(db_session, printer.id, 42)

        _, broadcast = await _sync(
            async_client, endpoint.format(id=printer.id), ams=LOADED_SLOT_PUSH, tray=_tray(tag="A" * 32), answers=77
        )

        assert await _slot(db_session, printer.id) == [77]
        assert _announced(broadcast) == [(printer.id, 0, 0)]

    async def test_a_new_slot_is_written_and_announced(
        self, endpoint, async_client: AsyncClient, printer_factory, db_session: AsyncSession, spoolman_enabled
    ):
        printer = await printer_factory(name="X1C")

        _, broadcast = await _sync(
            async_client, endpoint.format(id=printer.id), ams=LOADED_SLOT_PUSH, tray=_tray(tag="A" * 32), answers=77
        )

        assert await _slot(db_session, printer.id) == [77]
        assert _announced(broadcast) == [(printer.id, 0, 0)]

    async def test_an_unreadable_ledger_still_writes(
        self, endpoint, async_client: AsyncClient, printer_factory, db_session: AsyncSession, spoolman_enabled
    ):
        """Nothing to compare with: the write is the unconditional one it always was."""
        printer = await printer_factory(name="X1C")
        await _hand_assign(db_session, printer.id, 42)

        from backend.app.api.routes import spoolman as routes

        with patch.object(routes, "_read_slot_ledger", AsyncMock(return_value=None)):
            _, broadcast = await _sync(
                async_client, endpoint.format(id=printer.id), ams=LOADED_SLOT_PUSH, tray=_tray(tag="A" * 32), answers=77
            )

        assert await _slot(db_session, printer.id) == [77]
        assert _announced(broadcast) == [(printer.id, 0, 0)]
