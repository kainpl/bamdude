"""Describing an error on a printer BambuStudio does not package a catalogue for.

⚠️ **The gap, and how BambuStudio itself closes it.** BS packages seven
catalogues — `HMS.cpp`, `package_dev_id_types` = 093 094 20P 22E 239 26A 31B —
and they do not cover the fleet by serial prefix. A P1S reports 01P and an A1
mini reports 030. For any type outside that set BS does not guess and does not
merge models: it **fetches** the catalogue from `query.php?lang=…&d=<type>`.

Before the importer did the same, half the fleet had no descriptions at all and
an A1 mini fault reached the operator as a bare code, twice over:

    Помилка принтера: 12FF_0001
    3DP-030-102
    12FF_0001

⚠️ The per-model split stays absolute. 879 codes describe a different mechanism
on a different machine, so a model with no catalogue is answered by fetching its
own — never by borrowing another's.
"""

from __future__ import annotations

from backend.app.services import hms_catalogue

# Reported by an A1 mini (serial prefix 030).
RUNOUT_FULL = "12FF200000020001"
RUNOUT_SHORT = "12FF0001"


class TestTheFleetIsCovered:
    def test_the_types_bambustudio_does_not_package_are_shipped_too(self) -> None:
        """⚠️ Pinned against the files on disk. A re-import that silently
        dropped the fetched half would leave every P1S, X1 and A1 back where
        they were — with no description for anything."""
        shipped = set(hms_catalogue.shipped_devices())

        assert {"093", "094", "20P", "22E", "239", "26A", "31B"} <= shipped, "the packaged seven"
        assert {"00M", "00W", "01P", "01S", "030", "039", "03W"} <= shipped, "the fetched ones"

    def test_an_a1_mini_gets_its_own_description(self) -> None:
        assert hms_catalogue.describe("030", RUNOUT_FULL, RUNOUT_SHORT) == (
            "Filament at the spool holder has run out; please insert a new filament."
        )

    def test_and_a_p1s_answers_from_its_own(self) -> None:
        """⚠️ Deliberately NOT the A1 mini's code: the P1S catalogue does not
        carry 12FF… at all. Catalogues differ in which codes they hold, not
        only in wording, which is the second reason a model cannot be answered
        out of another's file."""
        heatbed = hms_catalogue.describe("01P", "0300010000010001", None)

        assert heatbed and "heatbed" in heatbed.lower()

    def test_an_unknown_type_answers_nothing_rather_than_borrowing(self) -> None:
        """⚠️ What BS does too: `_query_hms_msg` logs "there are no hms info for
        the device" and returns empty. Borrowing another model's text is how a
        two-nozzle machine gets told about the wrong hotend."""
        assert hms_catalogue.describe("ZZZ", RUNOUT_FULL, RUNOUT_SHORT) is None

    def test_a_missing_device_is_not_an_error(self) -> None:
        assert hms_catalogue.describe("", None, None) is None


class TestTheModelStillDecides:
    def test_two_models_can_describe_one_code_differently(self) -> None:
        """The reason the split exists at all — and the reason a fetch, not a
        merge, is the right way to fill a gap."""
        x2d = hms_catalogue.describe("20P", "0300020000010001", None)
        x1c = hms_catalogue.describe("31B", "0300020000010001", None)

        assert x2d and x1c and x2d != x1c
