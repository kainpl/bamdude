"""The overlay's token feed names the printer's model, in its display form.

``show=model`` on the overlay draws the model beside the printer's name. OBS
has only the ``overlay`` token, so the one extra fact rides the feed that token
already reads — the short display name, never the raw column, which may hold an
internal code or the long marketing name.
"""

import pytest

from backend.app.api.routes import printers
from backend.tests.integration.test_layer_projection_surfaces import live  # noqa: F401 -- shared fixture
from backend.tests.integration.test_layer_source_writers import storage  # noqa: F401 -- shared fixture


@pytest.mark.parametrize(
    ("stored", "shown"),
    [
        ("C12", "P1S"),
        ("C11", "P1P"),
        ("Bambu Lab X1 Carbon", "X1C"),
        ("H2D", "H2D"),
        ("Voron 2.4", "Voron 2.4"),
        (None, None),
    ],
)
async def test_a_printer_that_never_connected_names_its_display_model(db_session, printer_factory, stored, shown):
    printer = await printer_factory(model=stored)

    overlay = await printers.get_overlay_status(printer.id, db=db_session)

    assert overlay["connected"] is False
    assert overlay["model"] == shown


async def test_a_connected_printer_names_it_too(live, db_session):  # noqa: F811
    printer = live[0]

    overlay = await printers.get_overlay_status(printer.id, db=db_session)

    assert overlay["connected"] is True
    assert overlay["model"] is not None
    assert overlay["model"] == printers.normalize_model_name(printer.model)


async def test_the_feed_still_carries_no_serial_or_address(db_session, printer_factory):
    printer = await printer_factory(model="C12")

    overlay = await printers.get_overlay_status(printer.id, db=db_session)

    assert not {"serial_number", "ip_address", "access_code"} & set(overlay)
