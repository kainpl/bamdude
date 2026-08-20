"""Unit tests for the label template schema and its placeholder vocabulary."""

from __future__ import annotations

import typing

import pytest
from pydantic import ValidationError

from backend.app.services.label_template import (
    PLACEHOLDERS,
    BarcodeElement,
    LabelTemplateSpec,
    orientation,
    resolve,
)

CONTEXT = {
    "id": "42",
    "brand": "Polymaker",
    "material": "PLA",
    "subtype": "Matte",
    "color_name": "Ivory",
    "display_name": "Polymaker PLA Ivory",
    "deeplink": "https://bam.example/inventory?spool=42",
    "ean": "200000000042",
}


def test_a_minimal_template_validates():
    spec = LabelTemplateSpec(
        name="Test",
        width_mm=40,
        height_mm=20,
        elements=[
            {
                "type": "text",
                "x_mm": 2,
                "y_mm": 2,
                "w_mm": 24,
                "h_mm": 5,
                "content": "{display_name}",
            }
        ],
    )
    assert spec.shape == "rect"
    assert spec.elements[0].size_mm > 0


def test_the_element_type_is_what_picks_the_shape_of_the_rest():
    """A discriminated union, so a barcode's symbology cannot be set on a QR and
    a typo in ``type`` is caught here rather than at render time.
    """
    spec = LabelTemplateSpec(
        name="Test",
        width_mm=40,
        height_mm=20,
        elements=[
            {"type": "qr", "x_mm": 0, "y_mm": 0, "w_mm": 10, "h_mm": 10, "content": "{deeplink}"},
            {
                "type": "barcode",
                "x_mm": 0,
                "y_mm": 12,
                "w_mm": 30,
                "h_mm": 6,
                "content": "{ean}",
                "symbology": "ean13",
            },
        ],
    )
    assert spec.elements[0].type == "qr"
    assert spec.elements[1].symbology == "ean13"


def test_an_unknown_element_type_is_refused():
    with pytest.raises(ValidationError):
        LabelTemplateSpec(
            name="Test",
            width_mm=40,
            height_mm=20,
            elements=[{"type": "hologram", "x_mm": 0, "y_mm": 0, "w_mm": 5, "h_mm": 5}],
        )


def test_an_unknown_symbology_is_refused_by_the_schema_not_by_the_renderer():
    with pytest.raises(ValidationError):
        LabelTemplateSpec(
            name="Test",
            width_mm=40,
            height_mm=20,
            elements=[
                {
                    "type": "barcode",
                    "x_mm": 0,
                    "y_mm": 0,
                    "w_mm": 30,
                    "h_mm": 6,
                    "content": "{ean}",
                    "symbology": "aztec",
                }
            ],
        )


def test_the_schemas_symbology_list_is_the_renderers():
    """Two lists that have to agree, so assert that they do rather than hoping.
    A symbology the schema accepts and the renderer refuses is a template that
    saves and then fails to print.
    """
    from backend.app.services.label_barcode import SUPPORTED

    allowed = typing.get_args(BarcodeElement.model_fields["symbology"].annotation)
    assert set(allowed) == set(SUPPORTED)


@pytest.mark.parametrize("field", ["width_mm", "height_mm"])
def test_a_label_with_no_size_is_refused(field):
    kwargs = {"name": "Test", "width_mm": 40, "height_mm": 20, "elements": []}
    kwargs[field] = 0
    with pytest.raises(ValidationError):
        LabelTemplateSpec(**kwargs)


def test_an_element_with_no_size_is_refused():
    with pytest.raises(ValidationError):
        LabelTemplateSpec(
            name="Test",
            width_mm=40,
            height_mm=20,
            elements=[{"type": "text", "x_mm": 0, "y_mm": 0, "w_mm": 0, "h_mm": 5, "content": "x"}],
        )


def test_an_element_outside_the_label_validates_and_is_left_to_the_renderer():
    """Bleeding off the edge can be deliberate. Refusing it here would make the
    editor fight the user; the renderer clips and says so.
    """
    spec = LabelTemplateSpec(
        name="Test",
        width_mm=40,
        height_mm=20,
        elements=[{"type": "text", "x_mm": 35, "y_mm": 2, "w_mm": 20, "h_mm": 5, "content": "x"}],
    )
    assert spec.elements[0].x_mm + spec.elements[0].w_mm > spec.width_mm


def test_placeholders_are_substituted():
    assert resolve("{brand} · {material}", CONTEXT) == "Polymaker · PLA"


def test_an_unknown_placeholder_survives_verbatim():
    """The same choice the spool display-name setting already makes: a typo is
    visible in the preview rather than collapsing into a silent gap.
    """
    assert resolve("{brand} {colour_name}", CONTEXT) == "Polymaker {colour_name}"


def test_a_missing_value_becomes_empty_and_the_spacing_collapses():
    assert resolve("{brand} {subtype}", {"brand": "Polymaker", "subtype": ""}) == "Polymaker"


def test_text_with_no_placeholders_is_returned_unchanged():
    assert resolve("Spool", CONTEXT) == "Spool"


def test_the_vocabulary_carries_the_three_label_only_keys():
    keys = {p.key for p in PLACEHOLDERS}
    assert {"deeplink", "ean", "display_name"} <= keys


def test_the_vocabulary_matches_the_frontend_list_it_grew_from():
    """These seventeen come from ``frontend/src/utils/spoolName.ts``. The
    editor's field picker is served from here, so a key that exists on one side
    only is a field somebody can pick and never see filled in.
    """
    from_frontend = {
        "id",
        "brand",
        "material",
        "subtype",
        "color_name",
        "slicer_filament_name",
        "note",
        "label_weight_g",
        "label_weight_kg",
        "remaining_g",
        "remaining_kg",
        "remaining_pct",
        "color_hex",
        "cost_per_kg",
        "purchase_date",
        "filament_diameter",
        "lot",
    }
    assert from_frontend <= {p.key for p in PLACEHOLDERS}


def test_every_placeholder_has_something_to_show_in_a_picker():
    for p in PLACEHOLDERS:
        assert p.label and p.description and p.example, p.key


def test_placeholder_keys_are_unique():
    keys = [p.key for p in PLACEHOLDERS]
    assert len(keys) == len(set(keys))


def test_the_side_that_fits_the_head_is_the_side_that_goes_across_it():
    """⚠️ Print direction is derived, not stored. The community's own presets
    carry it per label — 40 × 12 as "left", 50 × 30 as "top" on one machine —
    and the rule underneath both is just which side fits.
    """
    head = 48.0
    assert orientation(40, 20, head) == "as_drawn"  # both fit; draw as authored
    assert orientation(80, 30, head) == "rotated"  # only the height fits
    assert orientation(40, 12, head) == "as_drawn"
    assert orientation(12, 40, head) == "as_drawn"


def test_a_label_that_fits_no_way_round_is_still_answered():
    """Refusing here would leave the caller with nothing to print and nothing to
    say. The renderer clips; the caller warns.
    """
    assert orientation(80, 60, 48.0) == "as_drawn"
