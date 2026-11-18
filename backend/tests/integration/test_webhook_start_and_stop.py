"""The webhook's control endpoints say what happened, and its start keeps the queue's gates.

``POST /webhook/printer/{id}/start`` never read whether the start path started anything and
answered "Print started" to every call; it also took the first pending job past a paused queue, an
uncleared plate and a schedule. ``/stop`` and ``/cancel`` had no test of the path that works.
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.printer_manager import printer_manager

pytestmark = pytest.mark.integration


async def _key(async_client: AsyncClient, **flags) -> dict:
    response = await async_client.post("/api/v1/api-keys/", json={"name": "hook", **flags})
    assert response.status_code == 200, response.text
    return {"X-API-Key": response.json()["key"]}


async def _call(async_client: AsyncClient, method: str, path: str, headers: dict):
    saved = async_client.headers.pop("Authorization", None)
    try:
        return await async_client.request(method, f"/api/v1/webhook{path}", headers=headers)
    finally:
        if saved is not None:
            async_client.headers["Authorization"] = saved


def _printer_reports(monkeypatch, state: str, *, awaiting_plate_clear: bool = False):
    monkeypatch.setattr(printer_manager, "get_status", lambda pid: SimpleNamespace(connected=True, state=state))
    monkeypatch.setattr(printer_manager, "is_awaiting_plate_clear", lambda pid: awaiting_plate_clear)
    monkeypatch.setattr(printer_manager, "ensure_fresh_connection", AsyncMock(return_value=True))


async def _queue(db_session, printer_id: int, **fields) -> PrinterQueue:
    queue = (await db_session.execute(select(PrinterQueue).where(PrinterQueue.id == printer_id))).scalar_one_or_none()
    if queue is None:
        queue = PrinterQueue(id=printer_id, printer_id=printer_id)
        db_session.add(queue)
    for name, value in fields.items():
        setattr(queue, name, value)
    await db_session.commit()
    return queue


async def _pending(db_session, printer_id: int, **fields) -> PrintQueueItem:
    item = PrintQueueItem(queue_id=printer_id, status="pending", position=fields.pop("position", 0), **fields)
    db_session.add(item)
    await db_session.commit()
    return item


@pytest.fixture
def start_path(monkeypatch):
    from backend.app.services.print_scheduler import scheduler

    start = AsyncMock(return_value=True)
    monkeypatch.setattr(scheduler, "_start_print", start)
    return start


class TestStart:
    async def test_a_started_print_is_reported_with_its_job(
        self, async_client, db_session, printer_factory, monkeypatch, start_path
    ):
        printer = await printer_factory()
        await _queue(db_session, printer.id)
        item = await _pending(db_session, printer.id)
        _printer_reports(monkeypatch, "IDLE")

        response = await _call(
            async_client, "POST", f"/printer/{printer.id}/start", await _key(async_client, can_control_printer=True)
        )

        assert response.status_code == 200, response.text
        assert response.json()["queue_item_id"] == item.id
        assert start_path.await_count == 1

    async def test_a_refused_start_is_not_reported_as_started(
        self, async_client, db_session, printer_factory, monkeypatch, start_path
    ):
        printer = await printer_factory()
        await _queue(db_session, printer.id)
        await _pending(db_session, printer.id, waiting_reason="No matching filament loaded")
        _printer_reports(monkeypatch, "IDLE")
        start_path.return_value = False

        response = await _call(
            async_client, "POST", f"/printer/{printer.id}/start", await _key(async_client, can_control_printer=True)
        )

        assert response.status_code == 409
        assert "No matching filament loaded" in response.json()["detail"]

    async def test_a_paused_queue_starts_nothing(
        self, async_client, db_session, printer_factory, monkeypatch, start_path
    ):
        printer = await printer_factory()
        await _queue(db_session, printer.id, is_paused=True)
        await _pending(db_session, printer.id)
        _printer_reports(monkeypatch, "IDLE")

        response = await _call(
            async_client, "POST", f"/printer/{printer.id}/start", await _key(async_client, can_control_printer=True)
        )

        assert response.status_code == 409
        assert start_path.await_count == 0

    async def test_an_uncleared_plate_starts_nothing(
        self, async_client, db_session, printer_factory, monkeypatch, start_path
    ):
        printer = await printer_factory()
        await _queue(db_session, printer.id)
        await _pending(db_session, printer.id)
        _printer_reports(monkeypatch, "FINISH", awaiting_plate_clear=True)

        response = await _call(
            async_client, "POST", f"/printer/{printer.id}/start", await _key(async_client, can_control_printer=True)
        )

        assert response.status_code == 409
        assert start_path.await_count == 0

    async def test_a_job_scheduled_for_later_is_not_the_next_one(
        self, async_client, db_session, printer_factory, monkeypatch, start_path
    ):
        printer = await printer_factory()
        await _queue(db_session, printer.id)
        later = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=3)
        await _pending(db_session, printer.id, position=0, scheduled_time=later)
        due = await _pending(db_session, printer.id, position=1)
        _printer_reports(monkeypatch, "IDLE")

        response = await _call(
            async_client, "POST", f"/printer/{printer.id}/start", await _key(async_client, can_control_printer=True)
        )

        assert response.status_code == 200, response.text
        assert response.json()["queue_item_id"] == due.id


class TestStop:
    @pytest.mark.parametrize("door", ["stop", "cancel"])
    @pytest.mark.parametrize("state", ["RUNNING", "PAUSE"])
    async def test_a_print_in_progress_is_stopped_once(self, async_client, printer_factory, monkeypatch, door, state):
        from backend.app.api.routes import webhook

        printer = await printer_factory()
        _printer_reports(monkeypatch, state)
        stop = _Recorder(True)
        monkeypatch.setattr(webhook, "stop_print_by_user", stop)

        response = await _call(
            async_client, "POST", f"/printer/{printer.id}/{door}", await _key(async_client, can_control_printer=True)
        )

        assert response.status_code == 200, response.text
        assert stop.calls == [printer.id]

    @pytest.mark.parametrize("door", ["stop", "cancel"])
    async def test_a_stop_that_was_not_sent_is_an_error(self, async_client, printer_factory, monkeypatch, door):
        from backend.app.api.routes import webhook

        printer = await printer_factory()
        _printer_reports(monkeypatch, "RUNNING")
        monkeypatch.setattr(webhook, "stop_print_by_user", _Recorder(False))

        response = await _call(
            async_client, "POST", f"/printer/{printer.id}/{door}", await _key(async_client, can_control_printer=True)
        )

        assert response.status_code == 503

    @pytest.mark.parametrize("door", ["stop", "cancel"])
    async def test_an_idle_printer_has_nothing_to_stop(self, async_client, printer_factory, monkeypatch, door):
        from backend.app.api.routes import webhook

        printer = await printer_factory()
        _printer_reports(monkeypatch, "IDLE")
        stop = _Recorder(True)
        monkeypatch.setattr(webhook, "stop_print_by_user", stop)

        response = await _call(
            async_client, "POST", f"/printer/{printer.id}/{door}", await _key(async_client, can_control_printer=True)
        )

        assert response.status_code == 409
        assert stop.calls == []


class _Recorder:
    def __init__(self, answer: bool):
        self.answer = answer
        self.calls: list[int] = []

    def __call__(self, printer_id, *args, **kwargs):
        self.calls.append(printer_id)
        return self.answer


async def test_the_queue_list_leaves_retired_printers_out(async_client, printer_factory):
    working = await printer_factory(name="Working")
    retired = await printer_factory(name="Retired", archived=True)

    response = await _call(async_client, "GET", "/queue", await _key(async_client, can_read_status=True))

    assert response.status_code == 200
    listed = {row["printer_id"] for row in response.json()}
    assert working.id in listed
    assert retired.id not in listed


def test_the_settings_card_lists_exactly_the_routes_that_exist():
    """The card in Settings is read off its source, not off a second list kept in a test."""
    from backend.app.api.routes.webhook import router

    page = Path(__file__).resolve().parents[3] / "frontend" / "src" / "pages" / "SettingsPage.tsx"
    shown = set(re.findall(r">/api/v1(/webhook/[^<]+)<", page.read_text(encoding="utf-8")))
    routes = {r.path.replace("{printer_id}", ":id") for r in router.routes}

    assert shown == routes
