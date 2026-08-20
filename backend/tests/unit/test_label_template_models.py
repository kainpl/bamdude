"""Schema-level guarantees for label templates and the sheets they print on."""

from __future__ import annotations

from types import SimpleNamespace

from reportlab.lib.pagesizes import A4, letter

from backend.app.core.auth import _APIKEY_DENIED_PERMISSIONS, _APIKEY_SCOPE_BY_PERMISSION
from backend.app.core.permissions import DEFAULT_GROUPS, Permission
from backend.app.models.label_template import LabelSheet, LabelTemplate
from backend.app.services.label_seed import BUILTIN_SHEETS, BUILTIN_TEMPLATES, STARTER_TEMPLATES
from backend.app.services.label_template import LabelSheetSpec, LabelTemplateSpec


def test_a_builtin_carries_its_old_api_name():
    """These four names are a public contract reachable by API key."""
    keys = {t["builtin_key"] for t in BUILTIN_TEMPLATES}
    assert keys == {"ams_holder_74x33", "ams_holder_75x55", "box_40x30", "box_62x29"}


def test_the_avery_names_became_sheets_not_templates():
    """They always described a page: size, a grid, margins and gaps. Filing them
    with the labels is what made half that list answer a different question.
    """
    assert {s["builtin_key"] for s in BUILTIN_SHEETS} == {"avery_5160", "avery_l7160"}
    assert not {t["builtin_key"] for t in BUILTIN_TEMPLATES} & {"avery_5160", "avery_l7160"}


def test_the_starters_cover_sizes_a_label_printer_can_actually_use():
    """⚠️ Three of the four built-ins are wider than a B1 can print (48 mm).
    Without these, somebody who plugs one in sees a list with nothing that fits.
    """
    sizes = {(t["width_mm"], t["height_mm"]) for t in STARTER_TEMPLATES}
    assert (40.0, 20.0) in sizes
    assert (50.0, 30.0) in sizes


def test_a_starter_keeps_its_content_within_what_a_b1_can_print():
    """⚠️ The template is the label, not the printable area: 50 × 30 stock is a
    50 × 30 template, because that is what the cassette says. But a B1's head is
    48 mm and aligns to one edge, so the last two millimetres of 50 mm stock are
    never printed — a starter has to keep its content inside that.

    Asserted on the elements rather than on the label size, which is the
    distinction the first version of this test got backwards.
    """
    for row in STARTER_TEMPLATES:
        for element in row["elements"]:
            assert element["x_mm"] + element["w_mm"] <= 48.0 + 1e-6, (row["name"], element["type"])


def test_a_starter_uses_the_whole_label():
    """⚠️ Measured, because looking at the numbers did not catch it.

    The first version of these starters capped the QR at 30% of the width,
    anchored it to the top, and stacked three fixed rows that stopped at
    11.8 mm — so a 40 × 20 label came off a real B1 with its bottom third blank
    and nothing anywhere said so. Every element was inside the label, which is
    all the other tests here check.
    """
    for row in STARTER_TEMPLATES:
        bottom = max(e["y_mm"] + e["h_mm"] for e in row["elements"])
        right = max(e["x_mm"] + e["w_mm"] for e in row["elements"])
        assert bottom >= row["height_mm"] * 0.85, (
            f"{row['name']}: content stops at {bottom} mm of {row['height_mm']} mm"
        )
        assert right >= min(row["width_mm"], 48.0) * 0.9, f"{row['name']}: content stops at {right} mm across"


def test_a_starter_row_is_only_placeholders():
    """⚠️ A caption survives the value it captions.

    "Lot {lot}" on a spool with no lot prints "Lot", which reads as a fault in
    the data rather than as an absent field. A row that resolves to nothing is
    skipped entirely — and that only works when there is nothing in the row but
    fields and separators.
    """
    import re

    for row in STARTER_TEMPLATES:
        for element in row["elements"]:
            if element["type"] != "text":
                continue
            leftovers = re.sub(r"\{[a-z_0-9]+\}", "", element["content"])
            words = re.findall(r"[A-Za-z]+", leftovers)
            assert not words or words == ["g"], (row["name"], element["content"], words)


def test_a_starter_has_no_builtin_key():
    """No API contract names them, and a key would make them undeletable."""
    assert all(t["builtin_key"] is None for t in STARTER_TEMPLATES)


def test_every_seeded_template_validates_against_the_schema():
    for row in [*BUILTIN_TEMPLATES, *STARTER_TEMPLATES]:
        LabelTemplateSpec(**{k: v for k, v in row.items() if k != "builtin_key"})


def test_every_seeded_sheet_validates_against_the_schema():
    for row in BUILTIN_SHEETS:
        LabelSheetSpec(**{k: v for k, v in row.items() if k != "builtin_key"})


def test_every_seeded_element_sits_inside_its_label():
    """A seeded design that warns on every render would teach people to ignore
    warnings, which is worse than the warning being wrong.
    """
    for row in [*BUILTIN_TEMPLATES, *STARTER_TEMPLATES]:
        spec = LabelTemplateSpec(**{k: v for k, v in row.items() if k != "builtin_key"})
        for element in spec.elements:
            assert element.x_mm >= 0 and element.y_mm >= 0, row["name"]
            assert element.x_mm + element.w_mm <= spec.width_mm + 1e-6, (row["name"], element.type)
            assert element.y_mm + element.h_mm <= spec.height_mm + 1e-6, (row["name"], element.type)


def test_a_builtin_carries_every_field_the_layout_it_replaces_drew():
    """The old layout draws brand, material, subtype, the colour hex, the name,
    the note and the spool id. A seed that drops one is a regression somebody
    notices at a shelf rather than in a test.
    """
    for row in BUILTIN_TEMPLATES:
        content = " ".join(e.get("content", "") for e in row["elements"])
        for placeholder in (
            "{brand}",
            "{material}",
            "{subtype}",
            "{color_hex}",
            "{display_name}",
            "{note}",
            "{id}",
        ):
            assert placeholder in content, (row["builtin_key"], placeholder)
        assert any(e["type"] == "qr" for e in row["elements"]), row["builtin_key"]
        assert any(e["type"] == "swatch" for e in row["elements"]), row["builtin_key"]


def test_a_builtin_truncates_rather_than_shrinking():
    """⚠️ The layout these replace keeps its type size and cuts. Shrinking
    instead inverts the hierarchy on real data — a long brand shrinks below the
    short material line under it — so the label reads as though the material
    were the more important field. Found by looking at a render.
    """
    for row in BUILTIN_TEMPLATES:
        for element in row["elements"]:
            if element["type"] == "text":
                assert element.get("fit") == "clip", (row["builtin_key"], element["content"])


def test_every_sheet_grid_fits_its_page():
    """⚠️ A grid that overruns its paper prints half a row, and that is only
    visible on paper.
    """
    for sheet in BUILTIN_SHEETS:
        page_w, page_h = A4 if sheet["page_size"] == "A4" else letter
        page_w_mm, page_h_mm = page_w / 72 * 25.4, page_h / 72 * 25.4
        used_w = (
            sheet["margin_left_mm"] + sheet["cols"] * sheet["cell_width_mm"] + (sheet["cols"] - 1) * sheet["gap_x_mm"]
        )
        used_h = (
            sheet["margin_top_mm"] + sheet["rows"] * sheet["cell_height_mm"] + (sheet["rows"] - 1) * sheet["gap_y_mm"]
        )
        assert used_w <= page_w_mm + 0.5, sheet["builtin_key"]
        assert used_h <= page_h_mm + 0.5, sheet["builtin_key"]


def test_a_builtin_key_is_unique_across_templates_and_sheets():
    """The label API resolves one name; two rows answering to it is a coin toss."""
    keys = [t["builtin_key"] for t in BUILTIN_TEMPLATES] + [s["builtin_key"] for s in BUILTIN_SHEETS]
    assert len(keys) == len(set(keys))


def test_a_sheet_row_does_not_reference_a_template():
    """⚠️ A sheet describes paper. Pointing at a design would make that design
    undeletable while a sheet looks at it, and weld one paper geometry to one
    layout forever.
    """
    assert not hasattr(LabelSheet, "template_id")
    assert all("template" not in key for row in BUILTIN_SHEETS for key in row)


def test_a_builtin_row_is_marked_as_one():
    """Called on the property directly: instantiating a mapped class here would
    configure every mapper in the app, which is a lot of machinery to prove one
    boolean.
    """
    assert LabelTemplate.is_builtin.fget(SimpleNamespace(builtin_key="box_40x30")) is True
    assert LabelTemplate.is_builtin.fget(SimpleNamespace(builtin_key=None)) is False


def test_the_new_permissions_each_land_in_exactly_one_api_key_map():
    for perm in (Permission.LABEL_TEMPLATES_READ, Permission.LABEL_TEMPLATES_WRITE):
        in_scope = perm in _APIKEY_SCOPE_BY_PERMISSION
        denied = perm in _APIKEY_DENIED_PERMISSIONS
        assert in_scope != denied, perm


def test_no_api_key_can_redesign_a_label():
    """A group permission, never a key one: there is no automation reason to
    redraw a label, and the consequence shows up on every one printed after.
    """
    assert Permission.LABEL_TEMPLATES_WRITE in _APIKEY_DENIED_PERMISSIONS


def test_operators_may_design_and_viewers_may_only_look():
    operators = set(DEFAULT_GROUPS["Operators"]["permissions"])
    viewers = set(DEFAULT_GROUPS["Viewers"]["permissions"])
    assert Permission.LABEL_TEMPLATES_WRITE.value in operators
    assert Permission.LABEL_TEMPLATES_READ.value in operators
    assert Permission.LABEL_TEMPLATES_READ.value in viewers
    assert Permission.LABEL_TEMPLATES_WRITE.value not in viewers
