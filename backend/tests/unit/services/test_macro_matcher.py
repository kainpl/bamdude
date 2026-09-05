"""One model rule for both doors a macro can be selected at.

``print_option_defaults._selected_macro_ids`` narrows the macro list when a
queue row is BUILT, and ``macro_matcher.find_macros_for_event`` narrows it again
when the event actually FIRES. Both go through ``macro_targets_model``, and for
a while only the first of them normalised the model it compared: a printer row
holding the long marketing name was offered every macro at dispatch and then
matched none of them at firing time. Nothing logged an error — "no macro targets
this printer" and "this printer fired nothing" are the same silence.
"""

from __future__ import annotations

import json

import pytest

from backend.app.models.macro import Macro
from backend.app.models.printer import Printer
from backend.app.services.macro_matcher import find_macros_for_event, macro_targets_model


def _macro(name: str, models: list[str], *, event: str = "print_finished") -> Macro:
    return Macro(
        name=name,
        printer_models=json.dumps(models),
        event=event,
        action_type="gcode",
        gcode="M400",
        enabled=True,
        swap_mode_only=False,
        swap_profile=None,
    )


def _printer(model: str | None) -> Printer:
    return Printer(name="bench", model=model, swap_mode_enabled=False, swap_profile=None)


class TestTheModelIsNormalisedBeforeItIsCompared:
    def test_a_long_named_printer_fires_the_macro_written_for_its_short_name(self) -> None:
        """The regression itself. ``Printer.model`` may hold "Bambu Lab X1
        Carbon"; the macro editor only ever writes "X1C"."""
        x1c = _macro("purge", ["X1C"])
        p1s = _macro("beep", ["P1S"])

        fired = find_macros_for_event("print_finished", _printer("Bambu Lab X1 Carbon"), [x1c, p1s])

        assert [m.name for m in fired] == ["purge"]

    def test_an_internal_code_resolves_to_the_same_short_name(self) -> None:
        """ "C12" is what a 3MF calls a P1S. Resolving the code map first is the
        whole reason ``normalize_model_name`` exists rather than
        ``normalize_printer_model``."""
        assert macro_targets_model(_macro("beep", ["P1S"]), "C12") is True

    def test_a_short_name_still_matches_itself(self) -> None:
        """Normalisation has to be a no-op for what the macro editor writes, or
        it would have traded one silent mismatch for another."""
        assert macro_targets_model(_macro("purge", ["X1C"]), "X1C") is True

    def test_a_different_model_still_does_not_match(self) -> None:
        assert macro_targets_model(_macro("beep", ["P1S"]), "Bambu Lab X1 Carbon") is False

    def test_the_wildcard_needs_no_model_at_all(self) -> None:
        assert macro_targets_model(_macro("any", ["*"]), None) is True

    @pytest.mark.parametrize("stored", ["not json at all", json.dumps({"X1C": True}), json.dumps("X1C")])
    def test_a_column_that_is_not_a_list_of_models_matches_nothing(self, stored: str) -> None:
        """Unchanged behaviour, pinned beside the change: normalising the model
        must not turn a malformed column into a match."""
        macro = _macro("purge", ["X1C"])
        macro.printer_models = stored

        assert macro_targets_model(macro, "X1C") is False

    def test_a_printer_with_no_model_matches_no_targeted_macro(self) -> None:
        assert macro_targets_model(_macro("purge", ["X1C"]), None) is False
