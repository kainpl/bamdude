"""The queue's log says why a printer sat out a pass (upstream 09b4584d, #3018).

``busy_printers`` holds two opposite facts: printers that could not take work
(running, offline, held by a plate or drying) and printers the pass has just
CLAIMED by dispatching to them. The summary called every one of them "not
available", reading the printer's state at logging time — so a report carried
"printer 1 not available — connected=True, state=IDLE" one line before
"Starting queue item 18" on that very printer. Every site that takes a printer
out of the pass now records why, and the summary tells a reservation from an
obstruction; the live fields stay, labelled as read now, not as the cause.

(The other half of #3018 — the queue calling an unscheduled item "ASAP" — is
not ours: the time column never said it.)
"""

from __future__ import annotations

import inspect
import logging
import re

from backend.app.services import print_scheduler
from backend.app.services.print_scheduler import PrintScheduler


class TestEverySiteSaysWhy:
    def test_no_printer_is_taken_out_of_the_pass_without_a_reason(self):
        # One add, inside mark_busy; every site goes through it.
        source = inspect.getsource(PrintScheduler.check_queue)
        assert len(re.findall(r"busy_printers\.add\(", source)) == 1
        helper = source[source.index("def mark_busy") :]
        assert helper.index("busy_printers.add(") < helper.index("\n\n")

    def test_the_reasons_are_the_closed_set(self):
        source = inspect.getsource(PrintScheduler.check_queue)
        reasons = set(re.findall(r'mark_busy\([^,]+,\s*"([a-z_]+)"\)', source))
        # The seed (active claims) is labelled before the loop runs.
        assert 'busy_reasons[claimed_printer_id] = "claimed"' in source
        assert reasons == {
            "dispatch_hold",
            "power_on_failed",
            "offline",
            "not_idle",
            "drying",
            "dispatched",
        }


class TestTheSummary:
    def _log(self, caplog, busy, reasons):
        with caplog.at_level(logging.INFO, logger=print_scheduler.logger.name):
            PrintScheduler._log_busy_printers(busy, reasons)
        return [r.getMessage() for r in caplog.records]

    def test_a_printer_dispatched_to_is_reserved_not_unavailable(self, caplog, monkeypatch):
        monkeypatch.setattr(print_scheduler.printer_manager, "get_status", lambda pid: None)
        lines = self._log(caplog, {1}, {1: "dispatched"})
        assert len(lines) == 1
        assert "not available" not in lines[0]
        assert "reserved" in lines[0]

    def test_an_obstruction_names_its_reason_and_the_state_as_read_now(self, caplog, monkeypatch):
        monkeypatch.setattr(print_scheduler.printer_manager, "get_status", lambda pid: None)
        monkeypatch.setattr(print_scheduler.printer_manager, "is_connected", lambda pid: False)
        monkeypatch.setattr(print_scheduler.printer_manager, "is_awaiting_plate_clear", lambda pid: False)
        lines = self._log(caplog, {2}, {2: "offline"})
        assert "offline" in lines[0]
        assert "now:" in lines[0]
