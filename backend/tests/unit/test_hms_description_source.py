"""Where BamDude's error text comes from, and what decides a notification.

Two separate things that used to be one.

⚠️ **The text** used to come from ``hms_errors.HMS_ERROR_DESCRIPTIONS`` — 853
entries lifted from ha-bambulab, keyed by short code and **model-agnostic**. It
disagrees with Bambu's own catalogue in 159 places, and not cosmetically:
``0300_401F`` is "The hotend is not installed" there and "The **right** hotend
is not installed" in Bambu's X2D catalogue. On a two-nozzle machine that is a
different fault.

⚠️ **The decision to notify** was gated twice: once on severity (fatal/serious,
which is the right question and already existed) and again on "do we have a
description for this", which is the same mistake the printer card made — using
"we have text" as a stand-in for "this is real". A fault could be fatal, fully
described by Bambu, and silent here because our smaller table had never heard
of it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from backend.app.services import hms_catalogue


class TestResolvingTheModel:
    def test_it_takes_the_serial_prefix(self) -> None:
        info = SimpleNamespace(serial_number="20P6BJ640901852")
        with patch("backend.app.services.printer_manager.printer_manager.get_printer", return_value=info):
            assert hms_catalogue.device_of(10) == "20P"

    def test_a_printer_we_have_no_info_for_answers_empty(self) -> None:
        """Empty, not a guess. A wrong model is worse than no description: 879
        codes describe different mechanisms on different machines."""
        with patch("backend.app.services.printer_manager.printer_manager.get_printer", return_value=None):
            assert hms_catalogue.device_of(10) == ""

    def test_a_serial_that_is_missing_or_short_does_not_raise(self) -> None:
        for serial in (None, "", "20"):
            info = SimpleNamespace(serial_number=serial)
            with patch("backend.app.services.printer_manager.printer_manager.get_printer", return_value=info):
                assert hms_catalogue.device_of(10) == (serial or "").upper()


class TestTheTextFollowsTheMachine:
    def test_the_same_code_reads_differently_on_two_models(self) -> None:
        """The point of the whole exercise, against the real shipped data.

        ⚠️ 325 codes differ between these two models alone. This one is a
        nozzle-temperature fault whose wording differs enough to matter when
        somebody is reading it at four in the morning.
        """
        code = "0300020000010001"
        x2d = hms_catalogue.describe("20P", code, None)
        x1c = hms_catalogue.describe("31B", code, None)

        assert x2d and x1c
        assert x2d != x1c, "the model-specific wording is gone"

    def test_our_old_text_is_not_what_bambu_says(self) -> None:
        """The 159 disagreements, in one example. Ours said "The hotend is not
        installed"; Bambu's X2D text names WHICH hotend, and the machine has
        two."""
        assert "right hotend" in (hms_catalogue.describe("20P", None, "0300401F") or "").lower()

    def test_the_code_that_started_this_now_has_text(self) -> None:
        """X2D, card full, timelapse refused. Reported by the printer, shown by
        BambuStudio, and previously invisible here."""
        assert hms_catalogue.describe("20P", "0500010000030004", None)


class TestTheOldTableIsGone:
    def test_it_is_neither_defined_nor_read_any_more(self) -> None:
        """A source check: the table was 900 lines and read as harmless
        reference data, so a future caller would have looked perfectly
        reasonable.

        ⚠️ Matches CODE, not prose — ``hms_errors.py`` still names the table in
        its docstring to explain where its descriptions went, and that mention
        is the opposite of a regression.
        """
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[2] / "app"
        uses = re.compile(r"HMS_ERROR_DESCRIPTIONS\s*(=|\.|\[)|import .*HMS_ERROR_DESCRIPTIONS")
        offenders = [
            path.relative_to(root).as_posix()
            for path in root.rglob("*.py")
            if uses.search(path.read_text(encoding="utf-8"))
        ]

        assert offenders == [], f"still using the ha-bambulab table: {offenders}"
