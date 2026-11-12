"""Repeating a finished print is new work under its order (WS-13 E13 R11, Q-05b).

The plate answer «Repeat» re-arms the finished queue row, and the printer prints it again
— a new physical print, counted under the row's order. So a row filed under an order asks
the Workshop's filing right for future work (``Fф``: ``orders:update`` or
``orders:file_prints``) and an open order, under the answer's locks, AFTER a receipt that was
already accepted is returned (a retry creates nothing new) and BEFORE the defects, the
receipt, the gate or the row are written: a refusal leaves everything as it was. The right
arrives from the door — the route's credentials, the Telegram chat's role — and the service
reads none itself. «Repeat without order» is the explicit choice made before sending: the
ROW loses its order, the finished print keeps its own.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select

from backend.app.models.archive import PrintArchive
from backend.app.models.print_completion_receipt import PrintCompletionReceipt
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.product import Product
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.tests.integration.test_workshop_library_rights import _jwt, _user

pytestmark = pytest.mark.integration

ROUTES_PM = "backend.app.api.routes.printers.printer_manager"
TOKEN = "gate-token"


def _finished_printer():
    return patch.multiple(
        ROUTES_PM,
        ensure_fresh_connection_for_printer=AsyncMock(return_value=True),
        is_connected=MagicMock(return_value=True),
        get_status=MagicMock(return_value=SimpleNamespace(state="FINISH")),
        set_awaiting_plate_clear=MagicMock(),
        is_awaiting_plate_clear=MagicMock(return_value=True),
    )


@pytest.fixture
async def held(db_session, printer_factory):
    """A finished print of an order's line waiting on the plate, its queue row held."""
    printer = await printer_factory()
    product = Product(name="Repeat lamp")
    order = Project(name="Repeat order")
    db_session.add_all([product, order])
    await db_session.flush()
    line = ProjectLine(project_id=order.id, product_id=product.id, quantity=3)
    db_session.add(line)
    await db_session.flush()
    archive = PrintArchive(
        printer_id=printer.id,
        filename="done.3mf",
        print_name="Done",
        file_path="x/done.3mf",
        file_size=1,
        status="completed",
        quantity=1,
        project_id=order.id,
        project_line_id=line.id,
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(archive)
    await db_session.flush()
    queue = PrinterQueue(id=printer.id, printer_id=printer.id, status="idle")
    db_session.add(queue)
    await db_session.flush()
    row = PrintQueueItem(
        queue_id=queue.id,
        archive_id=archive.id,
        status="completed",
        completed_at=datetime.now(timezone.utc),
        library_file_id=1,
        project_id=order.id,
        project_line_id=line.id,
    )
    db_session.add(row)
    stored = await db_session.get(Printer, printer.id)
    stored.awaiting_plate_clear = True
    stored.awaiting_plate_clear_archive_id = archive.id
    stored.awaiting_plate_clear_token = TOKEN
    await db_session.commit()
    return {"printer": printer.id, "archive": archive.id, "row": row.id, "order": order.id, "line": line.id}


def _body(held, **extra) -> dict:
    return {"expected_archive_id": held["archive"], "expected_gate_token": TOKEN, **extra}


async def _repeat(client, held, who: str | None = None, **extra):
    with _finished_printer():
        return await client.post(
            f"/api/v1/printers/{held['printer']}/repeat-print",
            json=_body(held, **extra),
            headers=await _jwt(who) if who else None,
        )


async def _state(db, held) -> dict:
    db.expire_all()
    row = await db.get(PrintQueueItem, held["row"])
    archive = await db.get(PrintArchive, held["archive"])
    printer = await db.get(Printer, held["printer"])
    receipts = await db.scalar(
        select(func.count(PrintCompletionReceipt.id)).where(PrintCompletionReceipt.archive_id == held["archive"])
    )
    return {
        "row": (row.status, row.project_id, row.project_line_id),
        "archive": (archive.project_id, archive.project_line_id, int(archive.defective_count or 0)),
        "gate": printer.awaiting_plate_clear,
        "receipts": receipts,
    }


@pytest.mark.asyncio
async def test_the_plate_operator_alone_is_refused_and_nothing_moves(committing_client, db_session, held):
    await _user(db_session, "pr_plate", ["printers:clear_plate"])
    before = await _state(db_session, held)
    r = await _repeat(committing_client, held, "pr_plate", defects={"defective_count": 1})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error"] == "filing_forbidden"
    assert await _state(db_session, held) == before


@pytest.mark.asyncio
async def test_the_filing_right_repeats_under_the_order_once(committing_client, db_session, held):
    await _user(db_session, "pr_desk", ["printers:clear_plate", "orders:file_prints"])
    first = await _repeat(committing_client, held, "pr_desk")
    assert first.status_code == 200, first.text
    state = await _state(db_session, held)
    assert state["row"] == ("pending", held["order"], held["line"])
    assert state["gate"] is False
    retry = await _repeat(committing_client, held, "pr_desk")
    assert retry.status_code == 200, retry.text
    assert retry.json()["item_id"] == first.json()["item_id"] == held["row"]
    assert (await _state(db_session, held))["receipts"] == 1


@pytest.mark.asyncio
async def test_a_closed_order_is_not_inherited_by_a_repeat(committing_client, db_session, held):
    order = await db_session.get(Project, held["order"])
    order.status = "completed"
    await db_session.commit()
    before = await _state(db_session, held)
    r = await _repeat(committing_client, held)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error"] == "order_closed"
    assert await _state(db_session, held) == before


@pytest.mark.asyncio
async def test_repeat_without_order_unfiles_only_the_row(committing_client, db_session, held):
    await _user(db_session, "pr_plate_bare", ["printers:clear_plate"])
    r = await _repeat(committing_client, held, "pr_plate_bare", without_order=True)
    assert r.status_code == 200, r.text
    state = await _state(db_session, held)
    assert state["row"] == ("pending", None, None)
    assert state["archive"][:2] == (held["order"], held["line"])


@pytest.mark.asyncio
async def test_the_plate_operators_repeat_without_order_is_one_print_however_often_it_is_sent(
    committing_client, db_session, held
):
    """O19 / R11: a duplicate of the operator's answer — the same card pressed again, or a second
    card that still thought the repeat kept the order — returns the first repeat's row and
    files nothing: one receipt, the row pending without the order."""
    await _user(db_session, "pr_plate_twice", ["printers:clear_plate"])
    first = await _repeat(committing_client, held, "pr_plate_twice", without_order=True)
    assert first.status_code == 200, first.text
    again = await _repeat(committing_client, held, "pr_plate_twice", without_order=True)
    assert again.status_code == 200, again.text
    stale = await _repeat(committing_client, held, "pr_plate_twice")
    assert stale.status_code == 200, stale.text
    assert first.json()["item_id"] == again.json()["item_id"] == stale.json()["item_id"] == held["row"]
    state = await _state(db_session, held)
    assert state["receipts"] == 1
    assert state["row"] == ("pending", None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("perms", "may_file"),
    [
        ({"printers:clear_plate"}, False),
        ({"printers:clear_plate", "orders:update"}, True),
        ({"printers:clear_plate", "orders:file_prints"}, True),
    ],
)
async def test_telegram_passes_the_chats_filing_right(perms, may_file):
    from backend.app.services.telegram_handlers import actions

    mod = "backend.app.services.telegram_handlers.actions"

    class _Session:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *_):
            return None

    callback = MagicMock()
    callback.data = "action:repeat_print:5:77"
    callback.answer = AsyncMock()
    answer_run = AsyncMock()
    with (
        patch(f"{mod}.has_perm", MagicMock(side_effect=lambda _chat, perm: perm in perms)),
        patch(f"{mod}.get_language", AsyncMock(return_value="en")),
        patch(f"{mod}.deny_out_of_scope", AsyncMock(return_value=False)),
        patch("backend.app.core.database.async_session", MagicMock(return_value=_Session())),
        patch("backend.app.services.telegram_handlers.printers.show_printer_detail", AsyncMock()),
        patch("backend.app.services.plate_answers.answer_plate_run", answer_run),
    ):
        await actions.cb_repeat_print(callback, MagicMock())
    assert answer_run.await_args.kwargs["may_file_future"] is may_file


@pytest.mark.asyncio
async def test_telegram_says_why_a_repeat_under_an_order_is_refused():
    from backend.app.services.plate_answers import FilingRefused
    from backend.app.services.telegram_handlers import actions

    mod = "backend.app.services.telegram_handlers.actions"

    class _Session:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *_):
            return None

    callback = MagicMock()
    callback.data = "action:repeat_print:5:77"
    callback.answer = AsyncMock()
    refused = AsyncMock(side_effect=FilingRefused(403, {"error": "filing_forbidden", "message": "no"}))
    with (
        patch(f"{mod}.has_perm", MagicMock(return_value=True)),
        patch(f"{mod}.get_language", AsyncMock(return_value="en")),
        patch(f"{mod}.deny_out_of_scope", AsyncMock(return_value=False)),
        patch("backend.app.core.database.async_session", MagicMock(return_value=_Session())),
        patch("backend.app.services.plate_answers.answer_plate_run", refused),
    ):
        await actions.cb_repeat_print(callback, MagicMock())
    text = callback.answer.await_args.args[0]
    assert callback.answer.await_args.kwargs.get("show_alert") is True
    assert "order" in text.lower()


@pytest.mark.asyncio
async def test_the_waiting_print_names_the_order_a_repeat_would_file_under(committing_client, db_session, held):
    """The card offers «Repeat without order» from this (WS-13 E13 T17): the ROW's order —
    what a repeat files under — and whether it is still open."""
    with _finished_printer():
        r = await committing_client.get(f"/api/v1/printers/{held['printer']}/waiting-print")
    assert r.status_code == 200, r.text
    assert r.json()["repeat_order_code"] == f"OR-{held['order']:04d}"
    assert r.json()["repeat_order_open"] is True
    order = await db_session.get(Project, held["order"])
    order.status = "cancelled"
    await db_session.commit()
    with _finished_printer():
        closed = await committing_client.get(f"/api/v1/printers/{held['printer']}/waiting-print")
    assert closed.json()["repeat_order_open"] is False


@pytest.mark.asyncio
async def test_an_archive_says_whether_its_order_is_open(committing_client, db_session, held):
    """A reprint inherits the archive's order (ARC-08); the print dialog reads its status."""
    r = await committing_client.get(f"/api/v1/archives/{held['archive']}")
    assert r.status_code == 200, r.text
    assert r.json()["project_status"] == "active"
    assert "project_color" in r.json()
