"""Which plug a print's energy is charged to.

Two plugs on one printer is legitimate: ``controls_printer_power`` exists (#2629)
to mark an accessory — a light or a filter — on the same printer as the mains
feed. What is not legitimate is the start of a measurement reading one meter and
the end reading another, which two independent ``scalar_one_or_none()`` calls can
produce: it yields a plausible, wrong delta rather than a missing one.

One helper, one deterministic order, both ends provably the same row.
"""

import pytest

from backend.app.main import _energy_plug_for_printer


@pytest.mark.asyncio
async def test_the_power_controlling_plug_wins(db_session, smart_plug_factory, printer_factory):
    printer = await printer_factory()
    await smart_plug_factory(name="light", printer_id=printer.id, controls_printer_power=False)
    await smart_plug_factory(name="mains", printer_id=printer.id, controls_printer_power=True)

    chosen = await _energy_plug_for_printer(printer.id, db_session)

    assert chosen is not None
    assert chosen.name == "mains"


@pytest.mark.asyncio
async def test_two_power_plugs_do_not_raise(db_session, smart_plug_factory, printer_factory):
    """``scalar_one_or_none()`` raised MultipleResultsFound here, which lost the
    print's energy for a configuration the app otherwise allowed."""
    printer = await printer_factory()
    await smart_plug_factory(name="a", printer_id=printer.id, controls_printer_power=True)
    await smart_plug_factory(name="b", plug_type="mqtt", printer_id=printer.id, controls_printer_power=True)

    chosen = await _energy_plug_for_printer(printer.id, db_session)

    assert chosen is not None


@pytest.mark.asyncio
async def test_the_choice_is_stable_across_calls(db_session, smart_plug_factory, printer_factory):
    """The whole point: the start of a measurement and its end must agree."""
    printer = await printer_factory()
    await smart_plug_factory(name="a", printer_id=printer.id, controls_printer_power=True)
    await smart_plug_factory(name="b", plug_type="mqtt", printer_id=printer.id, controls_printer_power=True)

    first = await _energy_plug_for_printer(printer.id, db_session)
    second = await _energy_plug_for_printer(printer.id, db_session)

    assert first.id == second.id


@pytest.mark.asyncio
async def test_an_accessory_alone_is_still_returned(db_session, smart_plug_factory, printer_factory):
    """A printer whose only plug is marked as an accessory still gets its energy
    measured by it — there is nothing else, and refusing to measure would be a
    worse answer than measuring with what is there."""
    printer = await printer_factory()
    await smart_plug_factory(name="light", printer_id=printer.id, controls_printer_power=False)

    chosen = await _energy_plug_for_printer(printer.id, db_session)

    assert chosen is not None
    assert chosen.name == "light"


@pytest.mark.asyncio
async def test_no_plug_is_none_not_an_error(db_session, printer_factory):
    printer = await printer_factory()

    assert await _energy_plug_for_printer(printer.id, db_session) is None
