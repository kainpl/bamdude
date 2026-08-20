"""What a placeholder resolves to, for one spool.

⚠️ Every one of these is a number or a string that ends up on printed stock, so
a wrong one is discovered at a shelf rather than in an error log.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from backend.app.services.label_context import example_context, spool_context, spoolman_context
from backend.app.services.label_template import PLACEHOLDERS, resolve

BASE = "https://bamdude.local"


def _spool(**overrides):
    """A spool-shaped object. The builder only reads attributes, so this avoids
    configuring every mapper in the app to prove some arithmetic.
    """
    fields = {
        "id": 42,
        "material": "PLA",
        "subtype": "Matte",
        "color_name": "Jade White",
        "rgba": "FF3300FF",
        "brand": "Polymaker",
        "label_weight": 1000,
        "weight_used": 250.0,
        "slicer_filament_name": "PolyTerra PLA",
        "note": "Kitchen shelf",
        "cost_per_kg": 22.5,
        "purchase_date": datetime(2026, 3, 14),
        "filament_diameter": "1.75",
        "lot": 3,
        "extra_colors": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_every_placeholder_gets_a_value():
    """⚠️ A missing key survives resolution verbatim, so a placeholder the
    builder forgets prints as ``{lot}`` on the label rather than as a gap.
    """
    context = spool_context(_spool(), deeplink_base=BASE)
    for placeholder in PLACEHOLDERS:
        assert placeholder.key in context, placeholder.key


def test_remaining_is_label_weight_minus_used():
    context = spool_context(_spool(), deeplink_base=BASE)
    assert context["remaining_g"] == "750"
    assert context["remaining_kg"] == "0.75"
    assert context["remaining_pct"] == "75%"


def test_an_overdrawn_spool_reads_zero_rather_than_negative():
    """AMS estimates drift past the nominal weight routinely; "-40 g" on a shelf
    label is noise, and a negative percentage is worse.
    """
    context = spool_context(_spool(weight_used=1040.0), deeplink_base=BASE)
    assert context["remaining_g"] == "0"
    assert context["remaining_pct"] == "0%"


def test_a_spool_with_no_nominal_weight_does_not_divide_by_it():
    context = spool_context(_spool(label_weight=0), deeplink_base=BASE)
    assert context["remaining_pct"] == "0%"
    assert context["label_weight_kg"] == "0"


def test_the_alpha_channel_is_dropped_from_the_hex():
    """It describes translucency. Printing ``#FF3300FF`` on a shelf reads as noise."""
    assert spool_context(_spool(), deeplink_base=BASE)["color_hex"] == "#FF3300"


def test_the_swatch_gets_every_colour_in_order():
    context = spool_context(_spool(extra_colors="00FF00, #0000FF"), deeplink_base=BASE)
    assert context["color_hex_all"] == "FF3300FF,00FF00,0000FF"


def test_a_colourless_spool_leaves_the_swatch_empty_rather_than_black():
    """An empty content string makes the canvas skip the element; a "#000000"
    fallback would print a black block on every spool nobody set a colour for.
    """
    context = spool_context(_spool(rgba=None), deeplink_base=BASE)
    assert context["color_hex_all"] == ""
    assert context["color_hex"] == ""


def test_the_display_name_from_the_screen_wins():
    """So the label matches the inventory row it was printed from."""
    context = spool_context(_spool(), deeplink_base=BASE, display_name="Shelf B · white")
    assert context["display_name"] == "Shelf B · white"


def test_without_one_the_old_fallback_chain_still_applies():
    assert spool_context(_spool(), deeplink_base=BASE)["display_name"] == "Jade White"
    assert spool_context(_spool(color_name=None), deeplink_base=BASE)["display_name"] == "PolyTerra PLA"


def test_the_deeplink_points_at_this_spool():
    assert spool_context(_spool(), deeplink_base=BASE)["deeplink"] == f"{BASE}/inventory?spool=42"


def test_the_ean_encodes_the_spool_id():
    assert spool_context(_spool(), deeplink_base=BASE)["ean"].endswith("042")


def test_an_id_too_long_to_encode_is_an_empty_barcode_not_a_failed_label():
    """The barcode element warns on its own when it cannot draw. Refusing the
    whole label because one field will not encode is worse than a gap.
    """
    context = spool_context(_spool(id=10**13), deeplink_base=BASE)
    assert context["ean"] == ""
    assert context["id"] == str(10**13)


def test_a_resolved_line_reads_as_written():
    context = spool_context(_spool(), deeplink_base=BASE)
    assert resolve("{brand} · {material} {subtype}", context) == "Polymaker · PLA Matte"


def test_an_empty_field_collapses_its_whitespace():
    """``{material} {subtype}`` on a spool with no subtype must not print a
    trailing space that pushes the text off-centre.
    """
    context = spool_context(_spool(subtype=None), deeplink_base=BASE)
    assert resolve("{material} {subtype}", context) == "PLA"


class TestSpoolman:
    """The same vocabulary, filled from a payload with a different shape."""

    @staticmethod
    def _payload(**overrides):
        payload = {
            "id": 7,
            "used_weight": 200.0,
            "comment": "top shelf",
            "filament": {
                "name": "PolyLite PLA Red",
                "material": "PLA",
                "color_hex": "#FF0000",
                "weight": 1000,
                "price": 19.99,
                "diameter": 1.75,
                "vendor": {"name": "Polymaker"},
            },
        }
        payload.update(overrides)
        return payload

    def test_every_placeholder_still_gets_a_value(self):
        """⚠️ Fields Spoolman does not carry resolve to empty rather than being
        omitted — an absent key prints as ``{lot}``.
        """
        context = spoolman_context(self._payload(), deeplink_base=BASE)
        for placeholder in PLACEHOLDERS:
            assert placeholder.key in context, placeholder.key

    def test_remaining_is_derived_when_spoolman_does_not_state_it(self):
        context = spoolman_context(self._payload(), deeplink_base=BASE)
        assert context["remaining_g"] == "800"

    def test_a_stated_remaining_wins_over_the_derived_one(self):
        """Spoolman knows about tares and manual corrections we do not."""
        context = spoolman_context(self._payload(remaining_weight=640.0), deeplink_base=BASE)
        assert context["remaining_g"] == "640"

    def test_a_payload_with_no_filament_still_renders(self):
        context = spoolman_context({"id": 3}, deeplink_base=BASE)
        assert context["display_name"] == "Spool"
        assert context["material"] == ""


def test_the_editor_previews_with_the_pickers_own_examples():
    """An editor that previewed with invented values would teach a layout
    against text nothing ever produces.
    """
    context = example_context()
    for placeholder in PLACEHOLDERS:
        if placeholder.key in ("deeplink", "color_hex_all"):
            continue
        assert context[placeholder.key] == placeholder.example


@pytest.mark.parametrize("key", [p.key for p in PLACEHOLDERS])
def test_no_placeholder_resolves_to_none(key):
    """``None`` reaches the renderer as the four characters "None"."""
    context = spool_context(_spool(), deeplink_base=BASE)
    assert isinstance(context[key], str)


class TestParityWithTheInventoryTable:
    """⚠️ The same tokens drive the spool display-name setting, interpolated
    client-side in ``frontend/src/utils/spoolName.ts``. A token that formats one
    way on screen and another on the label is found at a shelf, holding both.
    """

    def test_the_percentage_carries_its_sign(self):
        assert spool_context(_spool(), deeplink_base=BASE)["remaining_pct"].endswith("%")

    def test_a_weight_in_kilos_drops_trailing_zeros(self):
        """``formatKg`` prints 1 kg as "1", not "1.00"."""
        assert spool_context(_spool(label_weight=1000), deeplink_base=BASE)["label_weight_kg"] == "1"
        assert spool_context(_spool(label_weight=750), deeplink_base=BASE)["label_weight_kg"] == "0.75"

    def test_a_zero_weight_is_zero_rather_than_blank(self):
        """The screen shows "0"; a blank on the label would read as unknown."""
        assert spool_context(_spool(label_weight=0), deeplink_base=BASE)["label_weight_kg"] == "0"

    def test_a_free_spool_costs_zero_rather_than_nothing_at_all(self):
        assert spool_context(_spool(cost_per_kg=0), deeplink_base=BASE)["cost_per_kg"] == "0"

    def test_the_example_for_every_placeholder_is_shaped_like_a_real_value(self):
        """⚠️ The picker's examples are what the editor previews with. One that
        does not look like the real thing teaches a layout against text nothing
        produces — the percentage sign is exactly such a case.
        """
        context = spool_context(_spool(), deeplink_base=BASE)
        for placeholder in PLACEHOLDERS:
            real, example = context[placeholder.key], placeholder.example
            if not real or not example:
                continue
            assert real[0].isdigit() == example[0].isdigit(), placeholder.key
            assert real.endswith("%") == example.endswith("%"), placeholder.key


class TestSeparatorsWithNothingToSeparate:
    """⚠️ Found on printed stock, not in a test.

    A separator is punctuation between two things. With one of them gone it is
    debris, and debris on a shelf label reads as a fault in the data — which is
    exactly what somebody chases when they see it.
    """

    def test_a_missing_first_field_takes_the_separator_with_it(self):
        context = spool_context(_spool(brand=None), deeplink_base=BASE)
        assert resolve("{brand} · {material}", context) == "PLA"

    def test_a_missing_last_field_does_too(self):
        context = spool_context(_spool(material=None), deeplink_base=BASE)
        assert resolve("{brand} · {material}", context) == "Polymaker"

    def test_both_missing_leaves_nothing_at_all(self):
        """An element that resolves to nothing is skipped, so this is what makes
        an absent field absent rather than a lone dot on the label.
        """
        context = spool_context(_spool(purchase_date=None, lot=None), deeplink_base=BASE)
        assert resolve("{purchase_date} · {lot}", context) == ""

    def test_a_middle_field_going_missing_does_not_double_the_separator(self):
        context = spool_context(_spool(material=None), deeplink_base=BASE)
        assert resolve("{brand} · {material} · {subtype}", context) == "Polymaker · Matte"

    def test_a_hyphen_inside_a_value_is_not_a_separator(self):
        """⚠️ "PLA-CF" is a material, not two fields with punctuation between."""
        context = spool_context(_spool(material="PLA-CF"), deeplink_base=BASE)
        assert resolve("{material}", context) == "PLA-CF"

    def test_a_units_word_is_not_a_separator(self):
        """Only punctuation is dropped. "750 g" keeps its g."""
        context = spool_context(_spool(), deeplink_base=BASE)
        assert resolve("{remaining_g} g", context) == "750 g"

    def test_a_caption_beside_an_empty_field_still_survives(self):
        """⚠️ Which is precisely why the seeded rows carry no captions. This
        pins the limit of the rule rather than pretending it does more.
        """
        context = spool_context(_spool(lot=None), deeplink_base=BASE)
        assert resolve("Lot {lot}", context) == "Lot"
