"""An internal printer-model code has to reach the code map.

``normalize_printer_model`` returns unknown input **unchanged** rather than
None. So the obvious chain —

    normalize_printer_model(x) or normalize_printer_model_id(x)

— never reaches the second call: "C12" is not in the name map, comes back as
"C12", which is truthy. An auto-queue item targeting that code then matches no
printer row and waits for ever, saying only "No active C12 printers eligible"
(upstream `a9b57ccd`).

⚠️ The failure is silent and permanent: nothing errors, the item just never
routes. That is the same shape as an item with no ``target_model`` at all,
which is a documented no-op — so the symptom looks like a configuration
mistake rather than a bug.
"""

from __future__ import annotations

import pytest

from backend.app.utils.printer_models import normalize_model_name


class TestInternalCodes:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("C11", "P1P"),
            ("C12", "P1S"),
            ("C13", "X1E"),
            ("N6", "X2D"),
        ],
    )
    def test_a_code_resolves_to_the_short_name(self, code, expected):
        assert normalize_model_name(code) == expected

    def test_the_order_is_the_fix(self):
        """Proof the dead branch is dead, so the test fails if the order flips back."""
        from backend.app.utils.printer_models import normalize_printer_model

        assert normalize_printer_model("C12") == "C12", "the name map does not know codes"
        assert normalize_model_name("C12") == "P1S"


class TestEverythingElseIsUnchanged:
    def test_a_long_name_still_resolves(self):
        assert normalize_model_name("Bambu Lab X1 Carbon") == "X1C"

    def test_a_short_name_is_left_alone(self):
        assert normalize_model_name("P1S") == "P1S"

    def test_an_unknown_model_survives_rather_than_vanishing(self):
        """A machine we have never heard of is not a reason to lose the
        operator's answer — it just won't match a printer, which is honest."""
        assert normalize_model_name("Prusa MK4") == "Prusa MK4"

    def test_empty_input_is_none(self):
        assert normalize_model_name(None) is None
        assert normalize_model_name("") is None
