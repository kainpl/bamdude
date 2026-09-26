"""A print's energy is read from the first of the printer's plugs that measures
it, and its end from THAT plug (upstream 5a05e03c, #2859, adapted).

A printer may carry several plugs — the mains feed and an accessory (a filter,
lights, a script). The ranked first one was used whatever it could report, so a
mains switch with no power sensor ranked ahead of a metering accessory left the
print with no energy at all. The start now takes the first ranked plug that
reports a counter; the plug it used is remembered on the archive
(``extra_data["energy_plug_id"]``) and the end reads the same meter: two
different counters make a plausible, wrong delta rather than a missing one.
An archive started before this has no remembered plug and keeps the old rule —
the ranked first plug — at its end, which is the plug its start was read from.
"""

from unittest.mock import patch

import pytest

from backend.app import main
from backend.app.models.archive import PrintArchive


async def _archive(db_session, printer_id, **over):
    archive = PrintArchive(
        printer_id=printer_id,
        filename="a.3mf",
        file_path="x/a.3mf",
        file_size=1,
        status="printing",
        **over,
    )
    db_session.add(archive)
    await db_session.commit()
    await db_session.refresh(archive)
    return archive


def _meters(by_name: dict[str, float | None]):
    async def fake(plug, db, *, force_read=False):
        total = by_name.get(plug.name)
        return None if total is None else {"total": total}

    return patch.object(main, "_get_plug_energy", side_effect=fake)


def _no_snapshots():
    async def noop(*a, **k):
        return None

    return patch.object(main.smart_plug_manager, "record_energy_snapshot", side_effect=noop)


@pytest.mark.asyncio
async def test_the_start_passes_over_a_plug_that_measures_nothing(db_session, smart_plug_factory, printer_factory):
    printer = await printer_factory()
    await smart_plug_factory(name="mains", printer_id=printer.id, controls_printer_power=True)
    meter = await smart_plug_factory(
        name="meter", plug_type="mqtt", printer_id=printer.id, controls_printer_power=False
    )
    archive = await _archive(db_session, printer.id)

    with _meters({"mains": None, "meter": 12.5}), _no_snapshots():
        assert await main._record_energy_start(archive, printer.id, db_session) is True

    await db_session.refresh(archive)
    assert archive.energy_start_kwh == 12.5
    assert (archive.extra_data or {}).get("energy_plug_id") == meter.id


@pytest.mark.asyncio
async def test_the_end_reads_the_plug_the_start_used(db_session, smart_plug_factory, printer_factory, monkeypatch):
    printer = await printer_factory()
    await smart_plug_factory(name="mains", printer_id=printer.id, controls_printer_power=True)
    meter = await smart_plug_factory(
        name="meter", plug_type="mqtt", printer_id=printer.id, controls_printer_power=False
    )
    archive = await _archive(db_session, printer.id, energy_start_kwh=12.5, extra_data={"energy_plug_id": meter.id})
    monkeypatch.setattr(main, "async_session", lambda: _SessionProxy(db_session))

    # The mains plug reports now too — a different counter; it must not be read.
    with _meters({"mains": 900.0, "meter": 13.0}), _no_snapshots():
        await main._record_print_energy(archive.id, printer.id)

    await db_session.refresh(archive)
    assert archive.energy_kwh == 0.5


@pytest.mark.asyncio
async def test_a_remembered_plug_that_is_gone_records_nothing(
    db_session, smart_plug_factory, printer_factory, monkeypatch
):
    printer = await printer_factory()
    await smart_plug_factory(name="mains", printer_id=printer.id, controls_printer_power=True)
    archive = await _archive(db_session, printer.id, energy_start_kwh=12.5, extra_data={"energy_plug_id": 9999})
    monkeypatch.setattr(main, "async_session", lambda: _SessionProxy(db_session))

    with _meters({"mains": 900.0}), _no_snapshots():
        await main._record_print_energy(archive.id, printer.id)

    await db_session.refresh(archive)
    assert archive.energy_kwh is None


@pytest.mark.asyncio
async def test_an_archive_started_before_keeps_the_ranked_first_plug(
    db_session, smart_plug_factory, printer_factory, monkeypatch
):
    printer = await printer_factory()
    await smart_plug_factory(name="mains", printer_id=printer.id, controls_printer_power=True)
    await smart_plug_factory(name="meter", plug_type="mqtt", printer_id=printer.id, controls_printer_power=False)
    archive = await _archive(db_session, printer.id, energy_start_kwh=100.0)
    monkeypatch.setattr(main, "async_session", lambda: _SessionProxy(db_session))

    with _meters({"mains": 101.0, "meter": 13.0}), _no_snapshots():
        await main._record_print_energy(archive.id, printer.id)

    await db_session.refresh(archive)
    assert archive.energy_kwh == 1.0


class _SessionProxy:
    """``async with async_session() as db`` over the test's own session."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False
