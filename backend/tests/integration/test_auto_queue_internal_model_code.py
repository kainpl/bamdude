"""An auto-queue item targeting an internal model code must still find printers.

Bambu's own metadata names a machine by an internal code — "C12" is a P1S, "N6"
is an X2D — and those codes reach us from ``slice_info.config``, from the API,
and from anything that copies a value out of a 3MF. Routing compares
``target_model`` against ``Printer.model``, which holds the short name.

⚠️ The old chain — ``normalize_printer_model(x) or normalize_printer_model_id(x)``
— had a dead second branch: the first call returns unknown input **unchanged**,
so "C12" came back truthy and the code map was never reached. The item then
matched nothing and waited for ever behind "No active C12 printers eligible",
which reads exactly like an operator naming a model they do not own (upstream
`a9b57ccd`).

⚠️ The normalisation lives at the **read** site (``printers_for_item``) on
purpose: rows are written by the route, by telegram and by the virtual printer,
and one place that resolves the question covers all three — including whatever
writes the next one.
"""

import pytest

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.auto_queue_eligibility import printers_for_item


async def _printer_with_queue(db_session, printer_factory, *, model: str, serial: str):
    p = await printer_factory(name=model, serial_number=serial, model=model, is_active=True)
    db_session.add(PrinterQueue(printer_id=p.id, auto_distribute_eligible=True, is_paused=False))
    await db_session.commit()
    return p


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_internal_code_target_reaches_its_printers(db_session, printer_factory):
    p = await _printer_with_queue(db_session, printer_factory, model="P1S", serial="CODE01")

    printers, normalized, _suffix = await printers_for_item(db_session, AutoQueueItem(target_model="C12"))

    assert [x.id for x in printers] == [p.id]
    assert normalized == "P1S", "the reason string must name the machine the operator owns"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_short_name_target_is_unaffected(db_session, printer_factory):
    """The overwhelmingly common case — it must not regress for the rare one."""
    p = await _printer_with_queue(db_session, printer_factory, model="X1C", serial="CODE02")

    printers, normalized, _suffix = await printers_for_item(db_session, AutoQueueItem(target_model="X1C"))

    assert [x.id for x in printers] == [p.id]
    assert normalized == "X1C"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_model_nobody_owns_still_matches_nothing(db_session, printer_factory):
    """Normalising must not turn an honest empty answer into a wrong one."""
    await _printer_with_queue(db_session, printer_factory, model="P1S", serial="CODE03")

    printers, normalized, _suffix = await printers_for_item(db_session, AutoQueueItem(target_model="A1MINI"))

    assert printers == []
    assert normalized == "A1MINI"
