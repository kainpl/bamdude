"""skip_objects_supported must agree with the live 3MF gate, by construction.

The stored flags and the live read are two paths to one answer; if they ever
disagree the list badge contradicts what the preview says a line below it.
"""

import io
import json
import zipfile

import pytest

from backend.app.services.archive import extract_skip_support_from_3mf
from backend.app.services.library_helpers import skip_objects_supported_from_metadata


@pytest.mark.parametrize(
    "meta,expected",
    [
        ({"gcode_label_objects": True, "exclude_object": True}, True),
        ({"gcode_label_objects": True, "exclude_object": False}, False),
        ({"gcode_label_objects": False, "exclude_object": True}, False),
        ({"gcode_label_objects": False, "exclude_object": False}, False),
        # exclude_object absent — the parser omits it when the 3MF has no
        # interpretable value, and "unknown" must not read as "allowed".
        ({"gcode_label_objects": True}, False),
        ({}, False),
        (None, False),
    ],
)
def test_truth_table(meta, expected):
    assert skip_objects_supported_from_metadata(meta) is expected


@pytest.mark.parametrize(
    "glo,eo,expected",
    [(True, True, True), (True, False, False), (False, True, False), (False, False, False)],
)
def test_agrees_with_live_3mf_gate(glo, eo, expected):
    """Same 3MF, both paths, same answer.

    The live gate reads project_settings.config; the helper reads what the
    parser stored from it. Any divergence puts a badge in the list that the
    preview's own banner contradicts a line below.
    """
    settings_json = {"gcode_label_objects": glo, "exclude_object": eo}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/project_settings.config", json.dumps(settings_json))

    assert extract_skip_support_from_3mf(buf.getvalue()) is expected
    # What _extract_print_settings writes for this input: both keys present and
    # already coerced, so the stored dict is the settings dict.
    assert skip_objects_supported_from_metadata(settings_json) is expected


def test_schemas_expose_skip_objects_supported():
    """Every list/detail surface the badge renders on must carry the field.

    ``FileResponse`` is what ``routes/library.py`` imports as
    ``FileResponseSchema`` — the alias only exists to avoid colliding with
    Starlette's class of that name.
    """
    from backend.app.schemas.archive import ArchiveResponse
    from backend.app.schemas.library import FileListResponse, FileResponse

    for model in (FileListResponse, FileResponse, ArchiveResponse):
        assert "skip_objects_supported" in model.model_fields
        assert model.model_fields["skip_objects_supported"].default is False
