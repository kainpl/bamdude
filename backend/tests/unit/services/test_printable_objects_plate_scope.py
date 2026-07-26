"""The skip-objects list must be scoped to the plate that is printing (#2522).

``identify_id`` in a multi-plate 3MF is numbered per plate, so an unscoped
extract hands back plate 1's objects for a plate-3 job. That is not merely a
cosmetic mislabel: the user then skips by an id that belongs to a different
plate, and the printer cancels whatever really holds that id on the running one.

``extract_printable_objects_from_3mf`` has always accepted ``plate_number``;
what was missing was every caller except the archive-backed branch actually
passing it. These tests pin the extractor contract and the one call site that
can be driven without a live printer.
"""

from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backend.app.services.archive import (
    extract_printable_objects_from_3mf,
    load_objects_from_archive_into_state,
)

SLICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="index" value="1"/>
    <object identify_id="101" name="plate1-alpha" skipped="false"/>
    <object identify_id="102" name="plate1-beta" skipped="false"/>
  </plate>
  <plate>
    <metadata key="index" value="2"/>
    <object identify_id="201" name="plate2-gamma" skipped="false"/>
  </plate>
</config>
"""


def _make_3mf() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Metadata/slice_info.config", SLICE_INFO)
    return buf.getvalue()


class TestExtractorPlateScope:
    def test_plate_1_returns_only_its_own_objects(self):
        objects = extract_printable_objects_from_3mf(_make_3mf(), plate_number=1)
        assert set(objects) == {101, 102}

    def test_plate_2_returns_only_its_own_objects(self):
        objects = extract_printable_objects_from_3mf(_make_3mf(), plate_number=2)
        assert set(objects) == {201}
        # The plate-1 ids must NOT leak: skipping 101 while plate 2 prints would
        # cancel whatever object happens to carry that id there.
        assert 101 not in objects

    def test_unscoped_falls_back_to_the_first_plate(self):
        # Documents why passing the plate matters — this is what every caller
        # but one used to get.
        objects = extract_printable_objects_from_3mf(_make_3mf(), plate_number=None)
        assert set(objects) == {101, 102}


class TestArchiveReloadPassesThePlate:
    def test_reload_scopes_to_the_archive_plate(self, tmp_path):
        """load_objects_from_archive_into_state must use archive.plate_index."""
        threemf = tmp_path / "job.3mf"
        threemf.write_bytes(_make_3mf())

        archive = SimpleNamespace(id=7, file_path="job.3mf", plate_index=2, extra_data=None)
        client = MagicMock()
        client.state = SimpleNamespace(
            printable_objects={},
            printable_objects_bbox_all=None,
            skipped_objects=[],
            skip_objects_supported=False,
        )

        with (
            patch("backend.app.core.config.settings.base_dir", tmp_path),
            patch("backend.app.services.printer_manager.printer_manager") as pm,
        ):
            pm.get_client.return_value = client
            ok = load_objects_from_archive_into_state(archive, printer_id=1)

        assert ok is True
        assert set(client.state.printable_objects) == {201}, (
            "the plate-3 job got plate 1's object ids — skipping one would cancel the wrong object on the running plate"
        )
