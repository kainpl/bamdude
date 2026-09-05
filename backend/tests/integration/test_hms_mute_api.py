"""Hiding a stack entry is a per-printer, per-full-code decision that survives a
restart — the whole point is not seeing the same untextured code again after
every restart — and the status carries the hidden entries separately so the
modal can show and un-hide them. See ``services/hms_mute``.
"""

from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.hms_mute import HMSMutedEntry
from backend.app.services.hms_mute import forget, load_muted_codes, remember

pytestmark = pytest.mark.integration

CAMERA_FULL = "0500060000020070"


class TestTheService:
    async def test_remember_is_idempotent_and_load_returns_the_set(self, db_session, printer_factory):
        printer = await printer_factory()
        await remember(db_session, printer.id, CAMERA_FULL)
        await remember(db_session, printer.id, CAMERA_FULL)
        await remember(db_session, printer.id, "0300960000030001")
        await db_session.commit()

        assert await load_muted_codes(db_session, printer.id) == {CAMERA_FULL, "0300960000030001"}
        rows = (await db_session.execute(select(HMSMutedEntry))).scalars().all()
        assert len(rows) == 2

    async def test_forget_removes_only_what_it_is_told(self, db_session, printer_factory):
        printer = await printer_factory()
        await remember(db_session, printer.id, CAMERA_FULL)
        await remember(db_session, printer.id, "0300960000030001")
        await db_session.commit()

        assert await forget(db_session, printer.id, {CAMERA_FULL}) == 1
        await db_session.commit()
        assert await load_muted_codes(db_session, printer.id) == {"0300960000030001"}

    async def test_mutes_are_per_printer(self, db_session, printer_factory):
        a = await printer_factory()
        b = await printer_factory()
        await remember(db_session, a.id, CAMERA_FULL)
        await db_session.commit()

        assert await load_muted_codes(db_session, b.id) == set()


class TestTheRoutes:
    async def test_mute_persists_and_tells_the_live_client(
        self, async_client: AsyncClient, db_session, printer_factory
    ):
        printer = await printer_factory()
        with patch("backend.app.api.routes.printers.printer_manager") as pm:
            pm.apply_hms_mute = MagicMock(return_value=True)
            resp = await async_client.post(
                f"/api/v1/printers/{printer.id}/hms/mute", json={"full_code": CAMERA_FULL.lower()}
            )
        assert resp.status_code == 200, resp.text
        pm.apply_hms_mute.assert_called_once_with(printer.id, CAMERA_FULL)
        assert await load_muted_codes(db_session, printer.id) == {CAMERA_FULL}

    async def test_unmute_forgets_and_tells_the_live_client(
        self, async_client: AsyncClient, db_session, printer_factory
    ):
        printer = await printer_factory()
        await remember(db_session, printer.id, CAMERA_FULL)
        await db_session.commit()
        with patch("backend.app.api.routes.printers.printer_manager") as pm:
            pm.apply_hms_unmute = MagicMock(return_value=True)
            resp = await async_client.post(f"/api/v1/printers/{printer.id}/hms/unmute", json={"full_code": CAMERA_FULL})
        assert resp.status_code == 200, resp.text
        pm.apply_hms_unmute.assert_called_once_with(printer.id, CAMERA_FULL)
        assert await load_muted_codes(db_session, printer.id) == set()

    async def test_only_a_16_char_stack_code_is_accepted(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory()
        resp = await async_client.post(f"/api/v1/printers/{printer.id}/hms/mute", json={"full_code": "05004030"})
        assert resp.status_code == 422

    async def test_unknown_printer_is_404(self, async_client: AsyncClient):
        resp = await async_client.post("/api/v1/printers/999999/hms/mute", json={"full_code": CAMERA_FULL})
        assert resp.status_code == 404


class TestTheStatusCarriesHiddenEntries:
    async def test_hms_muted_is_reported_beside_hms_errors(self, async_client: AsyncClient, printer_factory):
        from backend.app.services.bambu_mqtt import HMSError, PrinterState

        printer = await printer_factory()
        state = PrinterState()
        state.connected = True
        state.state = "FINISH"
        state.hms_errors = [
            HMSError(code="0x30001", attr=0x03009600, module=3, severity=3, full_code="0300960000030001")
        ]
        state.hms_muted = [HMSError(code="0x20070", attr=0x05000600, module=5, severity=2, full_code=CAMERA_FULL)]

        with patch("backend.app.api.routes.printers.printer_manager") as pm:
            pm.get_status = MagicMock(return_value=state)
            pm.is_awaiting_plate_clear = MagicMock(return_value=False)
            resp = await async_client.get(f"/api/v1/printers/{printer.id}/status")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [e["full_code"] for e in body["hms_errors"]] == ["0300960000030001"]
        assert [e["full_code"] for e in body["hms_muted"]] == [CAMERA_FULL]
