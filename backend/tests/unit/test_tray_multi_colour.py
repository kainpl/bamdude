"""A spool can carry more than one colour, and we showed one of them.

Registry N5. A tray reports ``cols`` — a list of hex colours — and ``ctype``,
how they should be read. Both are parsed by BS in **both** of its tray parsers,
the AMS one and the external-spool one, so a multi-colour spool on the external
holder is not a separate case.

⚠️ **Two numbering traps, and they point opposite ways.**

``DevFilaColorType`` is ``CTYPE_MULTI = 0``, ``CTYPE_GRADIANT = 1``,
``CTYPE_SINGLE = 2`` — so a bare ``ctype: 0`` means *multi-colour*, not "no type
given", which is what a reader expecting 0-as-falsy would conclude.

And the drawing rule in ``AMSItem.cpp`` inverts the names again: ``CTYPE_MULTI``
draws a smooth **gradient** from the first colour to the last, while everything
else with more than one colour draws equal **bands**. Copied as found — it is
the reference behaviour, not a mistake to correct.

Showing one colour of several is not a rough label but a wrong one: a
black-and-white spool named "Black" is a spool somebody will pick for a black
print.
"""

from __future__ import annotations

import pytest

from backend.app.api.routes.printers import (
    FILA_CTYPE_MULTI,
    FILA_CTYPE_SINGLE,
    _tray_colours,
)


class TestTheEnumIsBsOwn:
    def test_the_values_are_not_what_the_names_suggest(self) -> None:
        """⚠️ Zero is MULTI. Anything that treats ``ctype`` as falsy-means-absent
        reads a multi-colour spool as untyped."""
        assert FILA_CTYPE_MULTI == 0
        assert FILA_CTYPE_SINGLE == 2


class TestTheFallbacksAreBsOwn:
    def test_no_cols_becomes_the_single_colour(self) -> None:
        """So callers never branch on which shape they got."""
        cols, _ = _tray_colours({}, "FF0000")

        assert cols == ["ff0000"]

    def test_no_ctype_is_derived_from_the_list(self) -> None:
        _, ctype = _tray_colours({"cols": ["#FF0000", "#00FF00"]}, None)

        assert ctype == FILA_CTYPE_MULTI

    def test_a_single_colour_derives_single(self) -> None:
        _, ctype = _tray_colours({}, "FF0000")

        assert ctype == FILA_CTYPE_SINGLE

    def test_an_explicit_ctype_wins_over_the_derivation(self) -> None:
        """The printer may call a two-colour spool something other than MULTI,
        and that changes how it is drawn."""
        _, ctype = _tray_colours({"cols": ["#FF0000", "#00FF00"], "ctype": FILA_CTYPE_SINGLE}, None)

        assert ctype == FILA_CTYPE_SINGLE

    def test_zero_is_honoured_as_a_real_value(self) -> None:
        """⚠️ The trap in one assertion: ``ctype: 0`` must survive, not be
        treated as missing and re-derived."""
        _, ctype = _tray_colours({"cols": ["#FF0000"], "ctype": 0}, None)

        assert ctype == FILA_CTYPE_MULTI


class TestTheShapeMatchesTheRestOfTheResponse:
    def test_the_hash_is_stripped_and_case_lowered(self) -> None:
        """``tray_color`` travels this way elsewhere in the same payload; two
        shapes for one kind of value is how a frontend grows special cases."""
        cols, _ = _tray_colours({"cols": ["#FF0000", "00FF00"]}, None)

        assert cols == ["ff0000", "00ff00"]

    def test_an_empty_tray_gets_an_empty_list(self) -> None:
        cols, _ = _tray_colours({}, None)

        assert cols == []

    @pytest.mark.parametrize("junk", ["notalist", 5, {"a": 1}])
    def test_a_non_list_cols_falls_back(self, junk) -> None:
        cols, _ = _tray_colours({"cols": junk}, "FF0000")

        assert cols == ["ff0000"]

    def test_non_string_entries_are_dropped(self) -> None:
        cols, _ = _tray_colours({"cols": ["#FF0000", None, 7, ""]}, None)

        assert cols == ["ff0000"]

    @pytest.mark.parametrize("junk", ["2", True, None])
    def test_a_non_int_ctype_is_derived_instead(self, junk) -> None:
        """``True`` is an int in Python and would silently mean GRADIENT."""
        _, ctype = _tray_colours({"cols": ["#FF0000", "#00FF00"], "ctype": junk}, None)

        assert ctype == FILA_CTYPE_MULTI
