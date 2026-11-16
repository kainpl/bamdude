"""The REST status and the WebSocket push carry the same ``next_pause``."""

import asyncio
from dataclasses import replace

import pytest

from backend.app.api.routes import printers
from backend.app.services import printer_manager as printer_manager_module
from backend.app.services.printer_manager import printer_state_to_dict
from backend.tests.integration.test_layer_projection_surfaces import live  # noqa: F401 -- shared fixture
from backend.tests.integration.test_layer_source_writers import storage  # noqa: F401 -- shared fixture
from backend.tests.unit.services.test_layer_projection import push


@pytest.mark.asyncio
@pytest.mark.integration
async def test_both_shapers_give_the_same_next_pause(live, db_session, monkeypatch):  # noqa: F811
    p, a, m, c, _ = live
    monkeypatch.setattr(printer_manager_module, "printer_manager", m)

    # The bind of a dispatched print has already started its analysis; nobody asked for it.
    context = m._print_file_analysis_contexts[p.id]
    for _ in range(300):
        if context.state == "ready":
            break
        await asyncio.sleep(0.01)
    assert context.state == "ready" and context.archive_id == a.id
    rest = (await printers._build_printer_status(p, db_session)).model_dump()
    assert rest["next_pause"] is None and printer_state_to_dict(c.state, p.id)["next_pause"] is None

    # The fixture's file has no pauses; give this print's analysis two.
    context.analysis = replace(context.analysis, pause_points=((20, 40), (300, 5)))
    push(c, mc_remaining_time=100)

    rest = (await printers._build_printer_status(p, db_session)).model_dump()
    ws = printer_state_to_dict(c.state, p.id)
    assert rest["next_pause"] == ws["next_pause"] == {"layer": 20, "minutes": 60}

    push(c, layer_num=20, mc_remaining_time=40)
    rest = (await printers._build_printer_status(p, db_session)).model_dump()
    assert rest["next_pause"] == printer_state_to_dict(c.state, p.id)["next_pause"] == {"layer": 300, "minutes": 35}
