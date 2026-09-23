"""A retired reel still sitting in the AMS is a KNOWN reel, not an unknown one.

After an AMS auto-switch the emptied reel stays in its slot with a readable
RFID tag (an X2D keeps reporting the slot occupied). Once that spool is
archived — by hand, or by the runout close-out (``runout_archive_spool_enabled``)
— the AMS sync must not bring it back: no auto-created duplicate, no tag
linked onto some other untagged spool, no "+ Add" prompt. The Spoolman sync
cache (``GET /spool``) cannot even see archived spools, so without a guard
the reel looks brand new on the very next AMS push.
"""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from backend.app import main as main_module
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.services.spoolman import SpoolmanClient

pytestmark = pytest.mark.integration

UUID = "A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4"
OTHER_UUID = "B1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4"


@pytest.fixture
def main_db(monkeypatch, db_session):
    @asynccontextmanager
    async def _session_ctx():
        yield db_session

    monkeypatch.setattr("backend.app.main.async_session", _session_ctx)


def _reel_in_slot(tray_uuid=UUID, tray_id=0, tag_uid="AABBCCDD11223344"):
    """A Bambu reel the AMS still reads — tag, type and colour intact, nothing left on it."""
    return {
        "id": tray_id,
        "tray_type": "PLA",
        "tray_sub_brands": "PLA Basic",
        "tray_color": "FF0000FF",
        "tray_info_idx": "GFA00",
        "tag_uid": tag_uid,
        "tray_uuid": tray_uuid,
        "tray_weight": "1000",
        "remain": 0,
    }


async def _run_on_ams_change(printer_id, ams_data):
    """Returns (broadcasts, auto_assign mock).

    ``auto_assign_spool`` is stubbed: its slot publish opens a second session,
    and on the harness's one shared in-memory connection closing that session
    ROLLS BACK the auto-created spool before the loop commits it (conftest,
    ``test_engine``). What these tests ask — was a spool created, was a tag
    linked, was an assignment attempted — is answered before that call.
    """
    status = SimpleNamespace(state="IDLE", raw_data={})
    with (
        patch("backend.app.services.ams_backup_compatibility_apply.rebuild_once", new=AsyncMock()),
        patch("backend.app.services.spool_tag_matcher.auto_assign_spool", new=AsyncMock()) as auto_assign,
        patch.object(main_module.printer_manager, "get_status", return_value=status),
        patch.object(main_module.ws_manager, "send_printer_status", new=AsyncMock()),
        patch.object(main_module.ws_manager, "broadcast", new=AsyncMock()) as broadcast,
        patch.object(main_module.mqtt_relay, "on_ams_change", new=AsyncMock()),
    ):
        await main_module.on_ams_change(printer_id, ams_data)
    return [c.args[0] for c in broadcast.await_args_list], auto_assign


async def _archived_spool(db_session):
    spool = Spool(
        material="PLA",
        subtype="Basic",
        brand="Bambu Lab",
        rgba="FF0000FF",
        label_weight=1000,
        weight_used=1000,
        tray_uuid=UUID,
        tag_uid="AABBCCDD11223344",
    )
    spool.archived_at = datetime.now(timezone.utc)
    db_session.add(spool)
    await db_session.commit()
    return spool


async def _spool_count(db_session):
    return (await db_session.execute(select(func.count(Spool.id)))).scalar_one()


class TestInternalInventory:
    @pytest.mark.asyncio
    async def test_the_archived_reel_is_not_created_again(self, db_session, printer_factory, main_db):
        printer = await printer_factory()
        await _archived_spool(db_session)

        sent, auto_assign = await _run_on_ams_change(printer.id, [{"id": 0, "tray": [_reel_in_slot()]}])

        assert await _spool_count(db_session) == 1
        auto_assign.assert_not_awaited()
        assert not any(m.get("type") == "unknown_tag" for m in sent)

    @pytest.mark.asyncio
    async def test_the_archived_reels_tag_is_not_linked_onto_another_spool(self, db_session, printer_factory, main_db):
        printer = await printer_factory()
        await _archived_spool(db_session)
        untagged = Spool(material="PLA", subtype="Basic", brand="Bambu Lab", rgba="FF0000FF", label_weight=1000)
        db_session.add(untagged)
        await db_session.commit()

        _sent, auto_assign = await _run_on_ams_change(printer.id, [{"id": 0, "tray": [_reel_in_slot()]}])

        await db_session.refresh(untagged)
        assert not untagged.tray_uuid
        auto_assign.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_reel_nobody_knows_is_still_created(self, db_session, printer_factory, main_db):
        """The guard is about ARCHIVED tags only — an unknown reel keeps today's auto-add."""
        printer = await printer_factory()
        await _archived_spool(db_session)

        _sent, auto_assign = await _run_on_ams_change(
            printer.id, [{"id": 0, "tray": [_reel_in_slot(tray_uuid=OTHER_UUID, tag_uid="99887766AABBCCDD")]}]
        )

        assert await _spool_count(db_session) == 2
        auto_assign.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_archived_third_party_tag_is_not_prompted_for(self, db_session, printer_factory, main_db):
        """A non-Bambu RFID reel has no auto-create at all — only the "+ Add" prompt, which it must not get."""
        printer = await printer_factory()
        spool = Spool(material="PETG", rgba="00FF00FF", label_weight=1000, tag_uid="11223344AABBCCDD")
        spool.archived_at = datetime.now(timezone.utc)
        db_session.add(spool)
        await db_session.commit()
        third_party = {
            "id": 0,
            "tray_type": "PETG",
            "tray_color": "00FF00FF",
            "tag_uid": "11223344AABBCCDD",
            "tray_uuid": "0" * 32,
            "tray_info_idx": "",
        }

        sent, _auto_assign = await _run_on_ams_change(printer.id, [{"id": 0, "tray": [third_party]}])

        assert not any(m.get("type") == "unknown_tag" for m in sent)


def _spoolman_client(active, archived):
    client = SpoolmanClient("http://spoolman.test")
    client.health_check = AsyncMock(return_value=True)
    client.get_spools = AsyncMock(return_value=active)
    client.get_all_spools = AsyncMock(return_value=active + archived)
    client.sync_ams_tray = AsyncMock(return_value=None)
    client.clear_location_for_removed_spools = AsyncMock(return_value=0)
    return client


def _spoolman_spool(spool_id, tag, archived):
    return {"id": spool_id, "archived": archived, "extra": {"tag": json.dumps(tag)}, "filament": {}}


class TestSpoolmanAutoSync:
    @pytest.fixture
    async def spoolman_on(self, db_session):
        db_session.add(Settings(key="spoolman_enabled", value="true"))
        db_session.add(Settings(key="spoolman_url", value="http://spoolman.test"))
        await db_session.commit()

    @pytest.mark.asyncio
    async def test_the_archived_reel_is_not_synced(self, db_session, printer_factory, main_db, spoolman_on):
        printer = await printer_factory()
        client = _spoolman_client(active=[], archived=[_spoolman_spool(8, UUID, archived=True)])

        with patch("backend.app.main.get_spoolman_client", AsyncMock(return_value=client)):
            sent, _auto_assign = await _run_on_ams_change(printer.id, [{"id": 0, "tray": [_reel_in_slot()]}])

        client.sync_ams_tray.assert_not_awaited()
        assert not any(m.get("type") == "unknown_tag" for m in sent)

    @pytest.mark.asyncio
    async def test_unknown_reels_still_sync_and_archived_spools_are_fetched_once(
        self, db_session, printer_factory, main_db, spoolman_on
    ):
        printer = await printer_factory()
        client = _spoolman_client(active=[], archived=[_spoolman_spool(8, UUID, archived=True)])
        trays = [_reel_in_slot(tray_uuid=OTHER_UUID, tray_id=0), _reel_in_slot(tray_uuid="C" * 32, tray_id=1)]

        with patch("backend.app.main.get_spoolman_client", AsyncMock(return_value=client)):
            await _run_on_ams_change(printer.id, [{"id": 0, "tray": trays}])

        assert client.sync_ams_tray.await_count == 2
        client.get_all_spools.assert_awaited_once_with(allow_archived=True)

    @pytest.mark.asyncio
    async def test_an_active_spool_with_the_tag_wins_and_nothing_extra_is_fetched(
        self, db_session, printer_factory, main_db, spoolman_on
    ):
        printer = await printer_factory()
        client = _spoolman_client(active=[_spoolman_spool(9, UUID, archived=False)], archived=[])

        with patch("backend.app.main.get_spoolman_client", AsyncMock(return_value=client)):
            await _run_on_ams_change(printer.id, [{"id": 0, "tray": [_reel_in_slot()]}])

        client.sync_ams_tray.assert_awaited_once()
        client.get_all_spools.assert_not_awaited()


class TestSpoolmanManualSync:
    """The two Sync buttons walk the same trays — and must not resurrect the reel either."""

    @pytest.fixture
    async def spoolman_on(self, db_session):
        db_session.add(Settings(key="spoolman_enabled", value="true"))
        db_session.add(Settings(key="spoolman_url", value="http://spoolman.test"))
        await db_session.commit()

    @staticmethod
    def _printer_manager():
        pm = SimpleNamespace()
        pm.get_status = lambda _pid: SimpleNamespace(
            state="IDLE", raw_data={"ams": [{"id": 0, "tray": [_reel_in_slot()]}]}
        )
        return pm

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/api/v1/spoolman/sync/{id}", "/api/v1/spoolman/sync-all"])
    async def test_the_archived_reel_is_not_synced(self, async_client, printer_factory, spoolman_on, path):
        printer = await printer_factory()
        client = _spoolman_client(active=[], archived=[_spoolman_spool(8, UUID, archived=True)])

        with (
            patch("backend.app.api.routes.spoolman.get_spoolman_client", AsyncMock(return_value=client)),
            patch("backend.app.api.routes.spoolman.printer_manager", self._printer_manager()),
        ):
            response = await async_client.post(path.format(id=printer.id))

        assert response.status_code == 200, response.text
        client.sync_ams_tray.assert_not_awaited()
        body = response.json()
        assert body["errors"] == []
        assert body["skipped"] == []
