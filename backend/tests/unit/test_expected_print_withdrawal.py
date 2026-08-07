"""An expected print whose command never went out must be withdrawn (upstream #2702 follow-up).

``register_expected_print`` necessarily runs *before* the print command — the
entry has to exist by the time the printer's first report arrives. Everything
between the two can still fail: a cancel, the strict-stagger refusal, a swap
macro, the calibration write, a dropped MQTT session, or ``start_print`` simply
returning False.

Nothing undid it. The only cleanup was the TTL sweeper at **two hours**, and
``_expected_prints`` is a *name-match adoption table*: ``on_print_start`` pops
``(printer_id, filename)`` and attaches the print to that archive. So a leftover
did not merely leak — for two hours it adopted **the next print of the same file
on that printer** into the wrong archive, however that print was started. On a
farm that repeats one file, that is the normal case rather than an edge one.
"""

from __future__ import annotations

import backend.app.core.database  # noqa: F401 — registers every mapper
import backend.app.models.printer_location  # noqa: F401
from backend.app.main import (
    _expected_print_creators,
    _expected_print_registered_at,
    _expected_prints,
    _print_ams_mappings,
    register_expected_print,
    withdraw_expected_print,
)

PRINTER = 7
FILENAME = "Benchy.3mf"


def _clear() -> None:
    for store in (_expected_prints, _expected_print_creators, _expected_print_registered_at, _print_ams_mappings):
        store.clear()


class TestWithdrawal:
    def test_every_filename_variant_registration_wrote_is_removed(self) -> None:
        """Registration writes three keys — the name, the stem, and the stem with
        ``.gcode``. Withdrawing only one would leave the others to match on, which
        is the whole failure this prevents."""
        _clear()
        register_expected_print(PRINTER, FILENAME, archive_id=42)
        assert len(_expected_prints) == 3

        withdraw_expected_print(PRINTER, FILENAME)

        assert _expected_prints == {}
        assert _expected_print_registered_at == {}

    def test_a_file_without_the_extension_is_handled(self) -> None:
        _clear()
        register_expected_print(PRINTER, "plate_1.gcode", archive_id=43)
        withdraw_expected_print(PRINTER, "plate_1.gcode")
        assert _expected_prints == {}

    def test_the_ams_mapping_goes_with_it(self) -> None:
        _clear()
        register_expected_print(PRINTER, FILENAME, archive_id=44, ams_mapping=[0, 1])
        assert _print_ams_mappings[44] == [0, 1]

        withdraw_expected_print(PRINTER, FILENAME)

        assert 44 not in _print_ams_mappings

    def test_the_creator_goes_with_it(self) -> None:
        _clear()
        register_expected_print(PRINTER, FILENAME, archive_id=45, created_by_id=3)
        assert _expected_print_creators

        withdraw_expected_print(PRINTER, FILENAME)

        assert _expected_print_creators == {}


class TestWithdrawalIsNarrow:
    def test_another_printer_expecting_the_same_file_is_untouched(self) -> None:
        """The farm case: the same file dispatched to several printers at once.
        Withdrawing one must not disarm the others."""
        _clear()
        register_expected_print(PRINTER, FILENAME, archive_id=50)
        register_expected_print(PRINTER + 1, FILENAME, archive_id=51)

        withdraw_expected_print(PRINTER, FILENAME)

        assert _expected_prints == {
            (PRINTER + 1, FILENAME): 51,
            (PRINTER + 1, "Benchy"): 51,
            (PRINTER + 1, "Benchy.gcode"): 51,
        }

    def test_a_different_file_on_the_same_printer_is_untouched(self) -> None:
        _clear()
        register_expected_print(PRINTER, FILENAME, archive_id=60, ams_mapping=[0])
        register_expected_print(PRINTER, "Other.3mf", archive_id=61, ams_mapping=[1])

        withdraw_expected_print(PRINTER, FILENAME)

        assert (PRINTER, "Other.3mf") in _expected_prints
        assert _print_ams_mappings == {61: [1]}

    def test_withdrawing_something_never_registered_is_a_no_op(self) -> None:
        """The happy path runs this on a cleared marker; it must not throw or
        disturb an unrelated entry."""
        _clear()
        register_expected_print(PRINTER, "Other.3mf", archive_id=70)

        withdraw_expected_print(PRINTER, FILENAME)

        assert (PRINTER, "Other.3mf") in _expected_prints


class TestTheAdoptionItPrevents:
    def test_a_stale_entry_would_otherwise_claim_the_next_print(self) -> None:
        """States the bug in terms of the table's actual use: ``on_print_start``
        pops by ``(printer_id, filename)``, so whatever sits there gets the print.
        """
        _clear()
        register_expected_print(PRINTER, FILENAME, archive_id=80)

        # Without the withdrawal, this is what the next start of the same file
        # on the same printer would find and adopt.
        assert _expected_prints.get((PRINTER, FILENAME)) == 80

        withdraw_expected_print(PRINTER, FILENAME)

        assert _expected_prints.get((PRINTER, FILENAME)) is None
