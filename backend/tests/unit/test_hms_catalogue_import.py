"""The importer takes both halves of BambuStudio's catalogue.

⚠️ The halves are keyed differently, and that is the whole reason the previous
import missed almost everything: ``device_hms`` rows carry a 16-character full
code, ``device_error`` rows an 8-character short code. BamDude's hardcoded table
was keyed on short codes, so it intersected ``device_error`` in 692 places and
``device_hms`` in **zero** — while ``device_hms`` is the half printers actually
report.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.import_hms_catalogue import build_catalogue, known_device_types

_DATA_DIR = Path(__file__).resolve().parents[2] / "app" / "data" / "hms"


class TestFlatteningOneFile:
    def test_both_halves_are_taken(self) -> None:
        source = {
            "data": {
                "device_hms": {"en": [{"ecode": "050002000002000B", "intro": "Long form"}]},
                "device_error": {"en": [{"ecode": "0580409C", "intro": "Short form"}]},
            }
        }

        assert build_catalogue(source, "en") == {
            "050002000002000B": "Long form",
            "0580409C": "Short form",
        }

    def test_a_missing_half_is_not_an_error(self) -> None:
        """Not every prefix ships both sections."""
        source = {"data": {"device_hms": {"en": [{"ecode": "AAAA", "intro": "Only half"}]}}}

        assert build_catalogue(source, "en") == {"AAAA": "Only half"}

    def test_an_entry_without_text_is_dropped(self) -> None:
        """An empty description is worse than none: it renders as a blank line
        where the "unrecognised code" fallback would have told the operator
        something."""
        source = {
            "data": {
                "device_hms": {
                    "en": [
                        {"ecode": "AAAA", "intro": ""},
                        {"ecode": "BBBB", "intro": "   "},
                        {"ecode": "CCCC", "intro": "Real"},
                    ]
                }
            }
        }

        assert build_catalogue(source, "en") == {"CCCC": "Real"}

    def test_a_file_with_neither_section_yields_nothing_rather_than_raising(self) -> None:
        assert build_catalogue({"data": {}}, "en") == {}
        assert build_catalogue({}, "en") == {}

    def test_a_language_the_file_does_not_carry_yields_nothing(self) -> None:
        """BS ships 16 languages and `uk` is not among them. Asking for one that
        is absent must be empty, not a crash and not English mislabelled."""
        source = {"data": {"device_hms": {"en": [{"ecode": "AAAA", "intro": "English"}]}}}

        assert build_catalogue(source, "uk") == {}


class TestTheGeneratedFiles:
    """Guards the generator's output, not just its function — a script that
    silently wrote ``{}`` would pass every test above."""

    def test_every_model_has_a_file_with_a_plausible_number_of_entries(self) -> None:
        for prefix in known_device_types():
            path = _DATA_DIR / f"{prefix}.json"
            assert path.exists(), f"{prefix}.json missing — run scripts/import_hms_catalogue.py"
            entries = json.loads(path.read_text(encoding="utf-8"))
            # ⚠️ One floor for two populations. The seven BambuStudio packages
            # carry ~5 000 codes each; the ones fetched from Bambu for the
            # older machines (00M, 01P, 030, …) carry ~2 500, because those
            # printers have fewer things to report. The threshold is here to
            # catch an import that produced a stub or nothing at all, not to
            # assert they are the same size.
            assert len(entries) > 1500, f"{prefix}.json has only {len(entries)} entries"

    def test_no_entry_is_blank(self) -> None:
        for prefix in known_device_types():
            entries = json.loads((_DATA_DIR / f"{prefix}.json").read_text(encoding="utf-8"))
            blank = [code for code, text in entries.items() if not text.strip()]
            assert not blank, f"{prefix}.json has blank descriptions: {blank[:5]}"

    def test_both_key_shapes_are_present(self) -> None:
        """⚠️ The check that would have caught the original mistake. Importing
        only the 8-character half is exactly what BamDude did for years."""
        entries = json.loads((_DATA_DIR / "20P.json").read_text(encoding="utf-8"))

        assert any(len(code) == 16 for code in entries), "no device_hms rows — the half printers report"
        assert any(len(code) == 8 for code in entries), "no device_error rows"

    def test_the_code_that_started_this_is_present(self) -> None:
        """X2D, "not enough space on the USB flash drive". Reported by the
        printer, shown by BambuStudio, invisible in BamDude."""
        entries = json.loads((_DATA_DIR / "20P.json").read_text(encoding="utf-8"))

        assert "0500010000030004" in entries
        assert "space" in entries["0500010000030004"].lower()
