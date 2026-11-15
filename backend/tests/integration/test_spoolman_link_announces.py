"""Tag link, tag unlink and create-from-slot tell open pages what they changed.

Each of them writes ``spoolman_slot_assignments`` and/or a spool's tag, and
none announced anything: another tab's printer card kept the previous spool and
its tag → spool map kept the previous holder until a poll (upstream 6c2731a6d).
The unlink also left the advertised-profile overlay entry of a slot that no
longer has a spool behind it — the unassign route forgets it for the very same
operation.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
from backend.app.services import ams_advertised_overlay as overlay

TAG = "A1B2C3D4E5F60718"


@pytest.fixture
async def spoolman_enabled(db_session: AsyncSession):
    from backend.app.models.settings import Settings

    db_session.add(Settings(key="spoolman_enabled", value="true"))
    db_session.add(Settings(key="spoolman_url", value="http://localhost:7912"))
    await db_session.commit()


def _spool(spool_id: int, **over) -> dict:
    return {
        "id": spool_id,
        "remaining_weight": 800.0,
        "used_weight": 200.0,
        "registered": "2026-09-01T10:00:00",
        "archived": False,
        "filament": {
            "id": 5,
            "name": "PLA Basic",
            "material": "PLA",
            "color_hex": "FF0000",
            "weight": 1000,
            "vendor": {"id": 1, "name": "Bambu Lab"},
        },
        "extra": {},
        **over,
    }


def _client(spool_id: int = 11) -> MagicMock:
    client = MagicMock()
    client.base_url = "http://localhost:7912"
    client.health_check = AsyncMock(return_value=True)
    client.merge_spool_extra = AsyncMock(return_value=None)
    # A Spoolman without native tags: nothing linked, nothing to take back.
    client.link_native_tags = AsyncMock(return_value=[])
    client.take_back_native_tags = AsyncMock()
    client.unlink_all_native_tags = AsyncMock()
    client.get_spools = AsyncMock(return_value=[])
    client.get_spool = AsyncMock(return_value=_spool(spool_id))
    return client


def _events(broadcast: AsyncMock) -> list[dict]:
    return [call.args[0] for call in broadcast.await_args_list if call.args]


def _slot_events(broadcast: AsyncMock) -> list[tuple[int, int, int]]:
    return [
        (m["printer_id"], m["ams_id"], m["tray_id"])
        for m in _events(broadcast)
        if m.get("type") == "spool_assignment_changed"
    ]


def _inventory_events(broadcast: AsyncMock) -> int:
    return sum(1 for m in _events(broadcast) if m.get("type") == "inventory_changed")


async def _rows(db: AsyncSession) -> list[tuple[int, int, int, int]]:
    result = await db.execute(
        select(
            SpoolmanSlotAssignment.printer_id,
            SpoolmanSlotAssignment.ams_id,
            SpoolmanSlotAssignment.tray_id,
            SpoolmanSlotAssignment.spoolman_spool_id,
        )
    )
    return [tuple(row) for row in result.all()]


async def _post(async_client: AsyncClient, path: str, client: MagicMock, json: dict | None = None, state=None):
    with (
        patch("backend.app.api.routes.spoolman.get_spoolman_client", AsyncMock(return_value=client)),
        patch("backend.app.api.routes.spoolman.printer_manager") as pm,
        patch("backend.app.core.websocket.ws_manager.broadcast", new_callable=AsyncMock) as broadcast,
    ):
        pm.get_client.return_value = None
        pm.get_status.return_value = state
        response = await async_client.post(path, json=json)
    return response, broadcast


@pytest.mark.asyncio
@pytest.mark.integration
class TestTagLinkAnnounces:
    async def test_a_link_with_a_printer_context_announces_the_tag_and_the_slot(
        self, async_client: AsyncClient, printer_factory, db_session: AsyncSession, spoolman_enabled
    ):
        printer = await printer_factory(name="X1C")

        response, broadcast = await _post(
            async_client,
            "/api/v1/spoolman/spools/11/link",
            _client(),
            {"spool_tag": TAG, "printer_id": printer.id, "ams_id": 0, "tray_id": 1},
        )

        assert response.status_code == 200
        assert await _rows(db_session) == [(printer.id, 0, 1, 11)]
        assert _inventory_events(broadcast) == 1
        assert _slot_events(broadcast) == [(printer.id, 0, 1)]

    async def test_a_link_without_a_printer_context_announces_only_the_tag(
        self, async_client: AsyncClient, spoolman_enabled
    ):
        response, broadcast = await _post(
            async_client, "/api/v1/spoolman/spools/11/link", _client(), {"spool_tag": TAG}
        )

        assert response.status_code == 200
        assert _inventory_events(broadcast) == 1
        assert _slot_events(broadcast) == []

    async def test_a_link_spoolman_refused_announces_nothing(self, async_client: AsyncClient, spoolman_enabled):
        from backend.app.services.spoolman import SpoolmanClientError

        client = _client()
        client.merge_spool_extra = AsyncMock(side_effect=SpoolmanClientError("no", 400))

        response, broadcast = await _post(async_client, "/api/v1/spoolman/spools/11/link", client, {"spool_tag": TAG})

        assert response.status_code == 502
        assert _events(broadcast) == []

    async def test_a_link_whose_native_tag_is_held_elsewhere_writes_and_announces_nothing(
        self, async_client: AsyncClient, printer_factory, db_session: AsyncSession, spoolman_enabled
    ):
        from backend.app.services.spoolman import NativeTagConflict

        printer = await printer_factory(name="X1C")
        client = _client()
        client.link_native_tags = AsyncMock(side_effect=NativeTagConflict("A" * 32, spool_id=7, archived=True))

        response, broadcast = await _post(
            async_client,
            "/api/v1/spoolman/spools/11/link",
            client,
            {"spool_tag": "A" * 32, "printer_id": printer.id, "ams_id": 0, "tray_id": 1},
        )

        assert response.status_code == 409
        client.merge_spool_extra.assert_not_awaited()
        assert await _rows(db_session) == []
        assert _events(broadcast) == []


@pytest.mark.asyncio
@pytest.mark.integration
class TestTagUnlinkAnnounces:
    async def test_an_unlink_announces_the_tag_and_every_slot_it_freed(
        self, async_client: AsyncClient, printer_factory, db_session: AsyncSession, spoolman_enabled
    ):
        printer = await printer_factory(name="X1C")
        db_session.add(SpoolmanSlotAssignment(printer_id=printer.id, ams_id=0, tray_id=1, spoolman_spool_id=11))
        db_session.add(SpoolmanSlotAssignment(printer_id=printer.id, ams_id=0, tray_id=2, spoolman_spool_id=12))
        await db_session.commit()

        response, broadcast = await _post(async_client, "/api/v1/spoolman/spools/11/unlink", _client())

        assert response.status_code == 200
        assert await _rows(db_session) == [(printer.id, 0, 2, 12)]
        assert _inventory_events(broadcast) == 1
        assert _slot_events(broadcast) == [(printer.id, 0, 1)]

    async def test_an_unlink_forgets_the_slots_advertised_overlay(
        self, async_client: AsyncClient, printer_factory, db_session: AsyncSession, spoolman_enabled
    ):
        printer = await printer_factory(name="X1C")
        db_session.add(SpoolmanSlotAssignment(printer_id=printer.id, ams_id=0, tray_id=1, spoolman_spool_id=11))
        await db_session.commit()
        kept, masked = object(), object()
        overlay.replace_printer(printer.id, {overlay.slot_key(0, 1): masked, overlay.slot_key(0, 2): kept})

        try:
            response, _ = await _post(async_client, "/api/v1/spoolman/spools/11/unlink", _client())
            assert response.status_code == 200
            assert overlay.entries_for(printer.id) == {overlay.slot_key(0, 2): kept}
        finally:
            overlay.forget_printer(printer.id)

    async def test_an_unlink_of_a_spool_in_no_slot_announces_only_the_tag(
        self, async_client: AsyncClient, spoolman_enabled
    ):
        response, broadcast = await _post(async_client, "/api/v1/spoolman/spools/11/unlink", _client())

        assert response.status_code == 200
        assert _inventory_events(broadcast) == 1
        assert _slot_events(broadcast) == []


@pytest.mark.asyncio
@pytest.mark.integration
class TestCreateFromSlotAnnounces:
    async def test_the_new_spool_and_its_slot_are_announced(
        self, async_client: AsyncClient, printer_factory, db_session: AsyncSession, spoolman_enabled
    ):
        printer = await printer_factory(name="X1C")
        client = _client()
        client.parse_ams_tray = MagicMock(return_value=SimpleNamespace(tray_id=1, tray_uuid="A" * 32, tag_uid=TAG))
        client.sync_ams_tray = AsyncMock(return_value={"id": 31})
        state = MagicMock()
        state.raw_data = {"ams": [{"id": 0, "tray": [{"id": 1, "tray_type": "PLA"}]}]}

        response, broadcast = await _post(
            async_client,
            "/api/v1/spoolman/spools/from-slot",
            client,
            {"printer_id": printer.id, "ams_id": 0, "tray_id": 1},
            state=state,
        )

        assert response.status_code == 200
        assert await _rows(db_session) == [(printer.id, 0, 1, 31)]
        assert _inventory_events(broadcast) == 1
        assert _slot_events(broadcast) == [(printer.id, 0, 1)]


@pytest.mark.asyncio
@pytest.mark.integration
class TestTheWeightSyncAnnounces:
    async def _sync(self, async_client: AsyncClient, tray: dict):
        client = _client()
        client.get_all_spools = AsyncMock(return_value=[_spool(11)])
        client.update_spool_full = AsyncMock(return_value=None)
        state = MagicMock()
        state.raw_data = {"ams": [{"id": 0, "tray": [tray]}]}
        with (
            patch("backend.app.api.routes.spoolman_inventory._get_client", new=AsyncMock(return_value=client)),
            patch("backend.app.api.routes.spoolman_inventory.printer_manager") as pm,
            patch("backend.app.core.websocket.ws_manager.broadcast", new_callable=AsyncMock) as broadcast,
        ):
            pm.get_status.return_value = state
            response = await async_client.post("/api/v1/spoolman/inventory/sync-ams-weights")
        assert response.status_code == 200
        return response.json(), broadcast

    async def test_a_weight_it_wrote_is_announced(
        self, async_client: AsyncClient, printer_factory, db_session: AsyncSession
    ):
        printer = await printer_factory(name="X1C")
        db_session.add(SpoolmanSlotAssignment(printer_id=printer.id, ams_id=0, tray_id=1, spoolman_spool_id=11))
        await db_session.commit()

        body, broadcast = await self._sync(async_client, {"id": 1, "remain": 60})

        assert body["synced"] == 1
        assert _inventory_events(broadcast) == 1

    async def test_nothing_written_announces_nothing(
        self, async_client: AsyncClient, printer_factory, db_session: AsyncSession
    ):
        printer = await printer_factory(name="X1C")
        db_session.add(SpoolmanSlotAssignment(printer_id=printer.id, ams_id=0, tray_id=1, spoolman_spool_id=11))
        await db_session.commit()

        body, broadcast = await self._sync(async_client, {"id": 1})

        assert body["synced"] == 0
        assert _inventory_events(broadcast) == 0
