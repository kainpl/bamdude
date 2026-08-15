"""Describing an error on a model BambuStudio ships no catalogue for.

⚠️ **The gap this closes.** BS ships seven catalogues — 093, 094, 20P, 22E, 239,
26A, 31B — and they do not cover the fleet by serial prefix. A P1S reports 01P
and an A1 mini reports 030; neither has a file, so every error on either
resolved to nothing. The operator got this in Telegram:

    Помилка принтера: 12FF_0001
    3DP-030-102
    12FF_0001

while the text sat in six of the seven catalogues, identical in all of them:
"Filament at the spool holder has run out; please insert a new filament."

⚠️ **Why this is not the merge the subsystem forbids.** The ban on merging
models exists because 879 codes describe a different mechanism on a different
machine. Unanimity is the test for whether a given code is one of those: where
every catalogue agrees, the model cannot be what decides the answer. Where they
disagree, we still say nothing rather than pick one.
"""

from __future__ import annotations

from unittest.mock import patch

from backend.app.services import hms_catalogue

# Reported by an A1 mini (serial prefix 030) and present, with identical text,
# in every catalogue that carries it.
RUNOUT_FULL = "12FF200000020001"
RUNOUT_SHORT = "12FF0001"


class TestAModelWeShipNoCatalogueFor:
    def test_the_fleet_really_is_uncovered(self) -> None:
        """Pinned against the shipped files, so a re-sync that adds 01P or 030
        makes this fail loudly rather than leaving dead fallback code."""
        shipped = hms_catalogue.shipped_devices()

        assert "20P" in shipped, "the X2D catalogue is the one that always existed"
        assert "030" not in shipped and "01P" not in shipped

    def test_it_still_gets_the_description(self) -> None:
        assert hms_catalogue.describe("030", RUNOUT_FULL, RUNOUT_SHORT) == (
            "Filament at the spool holder has run out; please insert a new filament."
        )

    def test_and_in_ukrainian(self) -> None:
        text = hms_catalogue.describe("01P", RUNOUT_FULL, RUNOUT_SHORT, "uk")

        assert text and "філамент" in text.lower()

    def test_an_unknown_model_is_not_an_error(self) -> None:
        assert hms_catalogue.describe("", RUNOUT_FULL, RUNOUT_SHORT)
        assert hms_catalogue.describe("ZZZ", None, None) is None


class TestWhereTheModelDoesDecide:
    def test_a_code_two_models_describe_differently_stays_unanswered(self) -> None:
        """⚠️ The load-bearing half. This code reads differently on a 20P than
        on a 31B, so for a model with no catalogue of its own there is no
        honest answer — and a plausible wrong one is worse than none."""
        assert hms_catalogue.describe("20P", "0300020000010001", None) is not None
        assert hms_catalogue.describe("31B", "0300020000010001", None) is not None

        assert hms_catalogue.describe("030", "0300020000010001", None) is None

    def test_the_models_own_catalogue_always_wins(self) -> None:
        """Consensus is consulted only after the model's own file has been
        asked — it never overrides a machine-specific answer."""
        own = hms_catalogue.describe("20P", "0300020000010001", None)
        others = hms_catalogue.describe("31B", "0300020000010001", None)

        assert own != others


class TestTheConsensusRule:
    def test_one_dissenting_catalogue_is_enough_to_refuse(self) -> None:
        catalogues = {
            "AAA": {"C0DE": "the same words"},
            "BBB": {"C0DE": "the same words"},
            "CCC": {"C0DE": "something else entirely"},
        }
        with (
            patch.object(hms_catalogue, "shipped_devices", return_value=sorted(catalogues)),
            patch.object(hms_catalogue, "_load", side_effect=lambda lang, dev: catalogues.get(dev, {})),
        ):
            assert hms_catalogue.describe("ZZZ", "C0DE", None) is None

    def test_agreement_answers(self) -> None:
        catalogues = {"AAA": {"C0DE": "the same words"}, "BBB": {"C0DE": "the same words"}}
        with (
            patch.object(hms_catalogue, "shipped_devices", return_value=sorted(catalogues)),
            patch.object(hms_catalogue, "_load", side_effect=lambda lang, dev: catalogues.get(dev, {})),
        ):
            assert hms_catalogue.describe("ZZZ", "C0DE", None) == "the same words"

    def test_nobody_knowing_it_is_still_none(self) -> None:
        with (
            patch.object(hms_catalogue, "shipped_devices", return_value=["AAA"]),
            patch.object(hms_catalogue, "_load", side_effect=lambda lang, dev: {}),
        ):
            assert hms_catalogue.describe("ZZZ", "C0DE", None) is None
