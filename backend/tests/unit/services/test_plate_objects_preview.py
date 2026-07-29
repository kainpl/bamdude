"""The read-only preview's payload builder.

Two axes that must stay independent: a file can list objects while forbidding
skipping (OrcaSlicer defaults exclude_object=false), and a file can allow
skipping with no gcode header (single-object Bambu Studio). Conflating them is
what this suite exists to prevent.
"""

import io
import json
import zipfile

from backend.app.services.archive import build_plate_objects_payload
from backend.tests.unit.services.test_plate_object_discovery import _png, make_3mf


def _with_settings(data: bytes, *, glo: bool, eo: bool, top: bool) -> bytes:
    """Re-pack a make_3mf blob with project_settings.config and an optional top view."""
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for item in src.namelist():
            z.writestr(item, src.read(item))
        z.writestr(
            "Metadata/project_settings.config",
            json.dumps({"gcode_label_objects": glo, "exclude_object": eo}),
        )
        if top:
            z.writestr("Metadata/top_1.png", _png([[(0, 0, 0)] * 4 for _ in range(4)]))
    return buf.getvalue()


def test_objects_listed_even_when_skipping_is_forbidden():
    """The regression that matters — the two axes are independent."""
    data = _with_settings(
        make_3mf(slice_ids={941: "part"}, gcode_ids=[941, 942, 943, 944, 945]),
        glo=True,
        eo=False,
        top=True,
    )
    payload = build_plate_objects_payload(data, 1)
    assert [o["id"] for o in payload["objects"]] == [941, 942, 943, 944, 945]
    assert payload["skip_objects_supported"] is False
    assert payload["has_top_view"] is True
    assert payload["plate_index"] == 1


def test_skipping_allowed_reads_true():
    data = _with_settings(make_3mf(slice_ids={7: "a"}), glo=True, eo=True, top=True)
    assert build_plate_objects_payload(data, 1)["skip_objects_supported"] is True


def test_missing_top_view_is_reported_not_guessed():
    """No image beats a 3/4 render with markers positioned for a top-down one."""
    data = _with_settings(make_3mf(slice_ids={7: "a"}), glo=True, eo=True, top=False)
    assert build_plate_objects_payload(data, 1)["has_top_view"] is False


def test_objects_are_sorted_by_id():
    data = _with_settings(
        make_3mf(slice_ids={941: "part"}, gcode_ids=[945, 941, 943]),
        glo=True,
        eo=True,
        top=True,
    )
    ids = [o["id"] for o in build_plate_objects_payload(data, 1)["objects"]]
    assert ids == sorted(ids)


def test_positions_flagged_approximate_when_no_pick_data():
    """Every marker falls to the frontend grid — the plate drawn is fiction."""
    data = _with_settings(
        make_3mf(slice_ids={941: "part"}, gcode_ids=[941, 942]),
        glo=True,
        eo=True,
        top=True,
    )
    payload = build_plate_objects_payload(data, 1)
    assert payload["positions_approximate"] is True
    assert all(o["norm"] is False for o in payload["objects"])


def test_unreadable_bytes_do_not_raise():
    """A corrupt archive yields an empty preview, never a 500."""
    payload = build_plate_objects_payload(b"not a zip", 1)
    assert payload["objects"] == []
    assert payload["has_top_view"] is False
    assert payload["skip_objects_supported"] is False
