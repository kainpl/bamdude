"""A manual Spoolman AMS sync tells open browsers which slots it changed.

Both sync endpoints maintain ``spoolman_slot_assignments`` — they upsert the
spool each synced tray resolved to and delete the rows of slots that came back
empty — and the printer card reads those rows through the
``spoolman-slot-assignments`` query. They announced nothing, so every other tab
kept showing the previous spool until something else refreshed it (upstream
``7363d5fd``: "its AMS sync writes the same row but announced nothing at all,
so there was no event to refresh on"; upstream maintains the ledger from its
AMS callback, ours only from these endpoints, so the event belongs here).

Only a slot whose row actually changed is named — the same rule the
unassign route follows: a sync that re-reads the spool already on file changes
nothing, and a runout (the row survives) announces nothing.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

EMPTY_SLOT_PUSH = [{"id": 0, "tray": [{"id": 0}]}]
LOADED_SLOT_PUSH = [{"id": 0, "tray": [{"id": 0, "tray_type": "PLA"}]}]


@pytest.fixture
async def spoolman_enabled(db_session: AsyncSession):
    from backend.app.models.settings import Settings

    db_session.add(Settings(key="spoolman_enabled", value="true"))
    db_session.add(Settings(key="spoolman_url", value="http://localhost:7912"))
    await db_session.commit()


def _client(parsed_tray=None, synced_spool_id: int | None = None):
    client = MagicMock()
    client.is_connected = True
    client.base_url = "http://localhost:7912"
    client.health_check = AsyncMock(return_value=True)
    client.get_spools = AsyncMock(return_value=[])
    client.parse_ams_tray = MagicMock(return_value=parsed_tray)
    client.sync_ams_tray = AsyncMock(return_value={"id": synced_spool_id} if synced_spool_id else None)
    return client


def _tagless_tray():
    return SimpleNamespace(
        tray_id=0,
        tray_uuid="",
        tag_uid="",
        tray_type="PLA",
        tray_color="FF0000FF",
        tray_sub_brands="PLA Basic",
    )


def _state(printer_state: str, ams: list):
    state = MagicMock()
    state.raw_data = {"ams": ams}
    state.state = printer_state
    return state


async def _printer_with_row(printer_factory, db_session: AsyncSession, spool_id: int = 42):
    printer = await printer_factory(name="X1C")
    db_session.add(SpoolmanSlotAssignment(printer_id=printer.id, ams_id=0, tray_id=0, spoolman_spool_id=spool_id))
    await db_session.commit()
    return printer


def _announced(broadcast: AsyncMock) -> list[tuple[int, int, int]]:
    return [
        (m["printer_id"], m["ams_id"], m["tray_id"])
        for m in (call.args[0] for call in broadcast.await_args_list)
        if m.get("type") == "spool_assignment_changed"
    ]


async def _sync(async_client: AsyncClient, path: str, client, state):
    with (
        patch("backend.app.api.routes.spoolman.get_spoolman_client", AsyncMock(return_value=client)),
        patch("backend.app.api.routes.spoolman.printer_manager") as pm,
        patch("backend.app.core.websocket.ws_manager.broadcast", new_callable=AsyncMock) as broadcast,
    ):
        pm.get_status.return_value = state
        response = await async_client.post(path)
    assert response.status_code == 200
    return broadcast


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("endpoint", ["/api/v1/spoolman/sync/{id}", "/api/v1/spoolman/sync-all"])
class TestTheSyncAnnouncesWhatItChanged:
    async def test_a_cleared_slot_is_announced(
        self, endpoint, async_client: AsyncClient, printer_factory, db_session: AsyncSession, spoolman_enabled
    ):
        printer = await _printer_with_row(printer_factory, db_session)
        broadcast = await _sync(
            async_client, endpoint.format(id=printer.id), _client(), _state("IDLE", EMPTY_SLOT_PUSH)
        )
        assert _announced(broadcast) == [(printer.id, 0, 0)]

    async def test_a_runout_changes_nothing_and_announces_nothing(
        self, endpoint, async_client: AsyncClient, printer_factory, db_session: AsyncSession, spoolman_enabled
    ):
        printer = await _printer_with_row(printer_factory, db_session)
        broadcast = await _sync(
            async_client, endpoint.format(id=printer.id), _client(), _state("RUNNING", EMPTY_SLOT_PUSH)
        )
        assert _announced(broadcast) == []

    async def test_reconnect_blank_preserves_assignment_until_replacement(
        self, endpoint, async_client: AsyncClient, printer_factory, db_session: AsyncSession, spoolman_enabled
    ):
        from sqlalchemy import select

        printer = await _printer_with_row(printer_factory, db_session, spool_id=42)
        path = endpoint.format(id=printer.id)
        spool_id = select(SpoolmanSlotAssignment.spoolman_spool_id).where(
            SpoolmanSlotAssignment.printer_id == printer.id
        )
        blank = [{"id": 0, "tray": [{"id": 0, "tray_type": "", "tray_color": "00000000", "exists": False, "state": 9}]}]
        assert _announced(await _sync(async_client, path, _client(), _state("RUNNING", blank))) == []
        assert await db_session.scalar(spool_id) == 42

        current = _client(parsed_tray=_tagless_tray(), synced_spool_id=42)
        assert _announced(await _sync(async_client, path, current, _state("RUNNING", LOADED_SLOT_PUSH))) == []
        assert await db_session.scalar(spool_id) == 42

        clear_tray = _tagless_tray()
        clear_tray.tray_type = "PETG"
        clear_tray.tray_color = "00000000"
        replacement = _client(parsed_tray=clear_tray, synced_spool_id=77)
        loaded = [{"id": 0, "tray": [{"id": 0, "tray_type": "PETG", "tray_color": "00000000", "exists": True}]}]
        assert _announced(await _sync(async_client, path, replacement, _state("RUNNING", loaded))) == [
            (printer.id, 0, 0)
        ]
        assert await db_session.scalar(spool_id) == 77

    async def test_a_slot_that_now_holds_another_spool_is_announced(
        self, endpoint, async_client: AsyncClient, printer_factory, db_session: AsyncSession, spoolman_enabled
    ):
        printer = await _printer_with_row(printer_factory, db_session, spool_id=42)
        client = _client(parsed_tray=_tagless_tray(), synced_spool_id=77)
        broadcast = await _sync(async_client, endpoint.format(id=printer.id), client, _state("IDLE", LOADED_SLOT_PUSH))
        assert _announced(broadcast) == [(printer.id, 0, 0)]

    async def test_re_reading_the_spool_on_file_announces_nothing(
        self, endpoint, async_client: AsyncClient, printer_factory, db_session: AsyncSession, spoolman_enabled
    ):
        printer = await _printer_with_row(printer_factory, db_session, spool_id=42)
        client = _client(parsed_tray=_tagless_tray(), synced_spool_id=42)
        broadcast = await _sync(async_client, endpoint.format(id=printer.id), client, _state("IDLE", LOADED_SLOT_PUSH))
        assert _announced(broadcast) == []
