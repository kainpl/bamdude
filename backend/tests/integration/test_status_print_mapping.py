"""The slot marks of a running print come from the print session's mapping, read-only.

Owner's decision Р13 (upstream cacaaf4da leg 2, adapted): both status shapers
show the dispatched slicer-slot → tray list the session holds — the one usage
attribution reads first — and never the live MQTT ``mapping`` AMS Filament
Backup rewrites, nor ``raw_data["ams_mapping"]``, which nothing here writes.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from backend.app.api.routes import printers
from backend.app.models.active_print_session import ActivePrintSession
from backend.app.services import print_mapping_projection, usage_tracker
from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.print_run_binding import bind_print_run, discard_print_run
from backend.app.services.printer_manager import printer_manager, printer_state_to_dict


def _running() -> PrinterState:
    state = PrinterState()
    state.connected = True
    state.state = "RUNNING"
    # What the firmware says now: backup has moved slot 1 from tray 1 to tray 6.
    state.raw_data = {"mapping": [6, 65535], "ams_mapping": [9, 9]}
    return state


def _session(printer_id: int, mapping, identity: str | None = "identity-1") -> usage_tracker.PrintSession:
    return usage_tracker.PrintSession(
        printer_id=printer_id,
        print_name="job",
        started_at=datetime.now(timezone.utc),
        ams_mapping=mapping,
        archive_identity=identity,
        owner_tracked=True,
    )


@pytest.fixture
def sessions(monkeypatch):
    held: dict = {}
    monkeypatch.setattr(usage_tracker, "_active_sessions", held)
    return held


@pytest.fixture
def bound():
    made: list[tuple[int, int]] = []

    def bind(printer_id: int, archive_id: int, identity: str):
        bind_print_run(
            printer_manager,
            printer_id=printer_id,
            archive_id=archive_id,
            archive_identity=identity,
            origin="dispatch",
        )
        made.append((printer_id, archive_id))

    yield bind
    for printer_id, archive_id in made:
        discard_print_run(printer_manager, printer_id, archive_id)


async def _rest(printer, db_session, monkeypatch, state):
    monkeypatch.setattr(printers.printer_manager, "get_status", lambda _id: state)
    return (await printers._build_printer_status(printer, db_session)).model_dump()


@pytest.mark.asyncio
@pytest.mark.integration
class TestTheMarksReadTheSessionMapping:
    async def test_both_shapers_show_the_dispatched_trays(self, printer_factory, db_session, monkeypatch, sessions):
        printer = await printer_factory(name="P", model="P1S")
        sessions[printer.id] = _session(printer.id, [1, -1, 5])
        state = _running()

        rest = await _rest(printer, db_session, monkeypatch, state)
        ws = printer_state_to_dict(state, printer.id, printer.model)

        assert rest["ams_mapping"] == ws["ams_mapping"] == [1, -1, 5]

    async def test_reading_changes_nothing_the_tracker_holds(self, printer_factory, db_session, monkeypatch, sessions):
        printer = await printer_factory(name="P", model="P1S")
        session = _session(printer.id, [1, -1, 5])
        sessions[printer.id] = session
        before = copy.deepcopy(session)
        state = _running()

        answer = (await _rest(printer, db_session, monkeypatch, state))["ams_mapping"]
        answer.append(99)
        printer_state_to_dict(state, printer.id, printer.model)["ams_mapping"].append(98)

        assert session == before
        assert sessions == {printer.id: session}

    async def test_without_a_session_in_memory_the_durable_row_answers_the_rest_read(
        self, printer_factory, db_session, monkeypatch, sessions, bound
    ):
        """Spoolman mode keeps no session in memory, and a restart loses it."""
        printer = await printer_factory(name="P", model="P1S")
        db_session.add(
            ActivePrintSession(
                printer_id=printer.id,
                print_name="job",
                started_at=datetime.now(timezone.utc),
                ams_mapping=[2, 3],
                archive_identity="identity-1",
                owner_tracked=True,
            )
        )
        await db_session.commit()
        bound(printer.id, 801, "identity-1")
        state = _running()

        rest = await _rest(printer, db_session, monkeypatch, state)
        ws = printer_state_to_dict(state, printer.id, printer.model)

        assert rest["ams_mapping"] == [2, 3]
        # The push cannot read the database; it leaves the loaded value alone.
        assert "ams_mapping" not in ws

    async def test_a_row_of_another_print_shows_nothing(
        self, printer_factory, db_session, monkeypatch, sessions, bound
    ):
        printer = await printer_factory(name="P", model="P1S")
        db_session.add(
            ActivePrintSession(
                printer_id=printer.id,
                print_name="older job",
                started_at=datetime.now(timezone.utc),
                ams_mapping=[2, 3],
                archive_identity="identity-older",
                owner_tracked=True,
            )
        )
        await db_session.commit()
        bound(printer.id, 802, "identity-now")

        rest = await _rest(printer, db_session, monkeypatch, _running())

        assert rest["ams_mapping"] == []

    async def test_a_row_with_no_bound_run_shows_nothing(self, printer_factory, db_session, monkeypatch, sessions):
        printer = await printer_factory(name="P", model="P1S")
        db_session.add(
            ActivePrintSession(
                printer_id=printer.id,
                print_name="job",
                started_at=datetime.now(timezone.utc),
                ams_mapping=[2, 3],
            )
        )
        await db_session.commit()

        rest = await _rest(printer, db_session, monkeypatch, _running())

        assert rest["ams_mapping"] == []

    async def test_a_session_of_another_archive_shows_nothing(
        self, printer_factory, db_session, monkeypatch, sessions, bound
    ):
        printer = await printer_factory(name="P", model="P1S")
        sessions[printer.id] = _session(printer.id, [1], identity="identity-older")
        bound(printer.id, 803, "identity-now")
        state = _running()

        rest = await _rest(printer, db_session, monkeypatch, state)
        ws = printer_state_to_dict(state, printer.id, printer.model)

        assert rest["ams_mapping"] == ws["ams_mapping"] == []

    async def test_an_idle_printer_has_no_marks(self, printer_factory, db_session, monkeypatch, sessions):
        printer = await printer_factory(name="P", model="P1S")
        sessions[printer.id] = _session(printer.id, [1])
        state = _running()
        state.state = "FINISH"

        rest = await _rest(printer, db_session, monkeypatch, state)
        ws = printer_state_to_dict(state, printer.id, printer.model)

        assert rest["ams_mapping"] == ws["ams_mapping"] == []


def test_a_mapping_that_is_not_a_list_of_tray_numbers_is_no_mapping():
    assert print_mapping_projection._clean(None) == []
    assert print_mapping_projection._clean("1,2") == []
    assert print_mapping_projection._clean([1, "2", None, True, -1]) == [1, -1]
