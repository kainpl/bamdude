"""The plate gate is released on a printer that is switched off (upstream 05d87a97, #2864).

With Auto Power Off the ordinary end of a print is the gate up and the printer
off. Clearing sends nothing to the printer — the gate is BamDude's own flag,
persisted exactly so it survives the power cycle — yet the route refused with
"Printer not connected" (or tried to reconnect first), and the card hid the
button under the same condition: nothing could release the gate short of
switching every printer back on. The status endpoint also reported a clean
plate for a printer with no cached state, hiding the control where it was
needed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.print_queue import PrintQueueItem
from backend.tests.integration.test_plate_answers_defects import ROUTES_PM, _finished_on

pytestmark = pytest.mark.integration


def _switched_off(awaiting: bool = True):
    return patch.multiple(
        ROUTES_PM,
        ensure_fresh_connection_for_printer=AsyncMock(side_effect=AssertionError("must not reconnect")),
        is_connected=MagicMock(return_value=False),
        get_status=MagicMock(return_value=None),
        set_awaiting_plate_clear=MagicMock(),
        is_awaiting_plate_clear=MagicMock(return_value=awaiting),
    )


@pytest.mark.asyncio
async def test_clear_plate_on_a_switched_off_printer_releases_the_gate(async_client, printer_factory, db_session):
    printer = await printer_factory()
    _archive, row = await _finished_on(db_session, printer, {"lid": 1})
    row_id = row.id

    with _switched_off():
        resp = await async_client.post(f"/api/v1/printers/{printer.id}/clear-plate")

    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert (
        await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.id == row_id))
    ).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_a_connected_printer_that_is_printing_still_refuses(async_client, printer_factory, db_session):
    from types import SimpleNamespace

    printer = await printer_factory()
    await _finished_on(db_session, printer, {"lid": 1})
    with patch.multiple(
        ROUTES_PM,
        ensure_fresh_connection_for_printer=AsyncMock(return_value=True),
        is_connected=MagicMock(return_value=True),
        get_status=MagicMock(return_value=SimpleNamespace(state="RUNNING")),
        set_awaiting_plate_clear=MagicMock(),
        is_awaiting_plate_clear=MagicMock(return_value=True),
    ):
        resp = await async_client.post(f"/api/v1/printers/{printer.id}/clear-plate")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_status_of_a_printer_with_no_state_carries_the_gate(async_client, printer_factory):
    printer = await printer_factory()
    with _switched_off(awaiting=True):
        resp = await async_client.get(f"/api/v1/printers/{printer.id}/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["connected"] is False
    assert body["awaiting_plate_clear"] is True
