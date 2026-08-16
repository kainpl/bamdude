"""``attach_3mf_to_archive`` has to fill what ``archive_print`` fills.

The two are the only ways a 3MF's metadata reaches an archive row, and since
``on_print_start`` began creating the row up front and filling it in later,
the attach is the path **every external print** takes — not just the rare
recovered one. Anything ``archive_print`` writes and the attach forgets is a
column that is simply NULL for that whole class of print.

Three were forgotten, and each is silent in a different way:

* ``bed_type`` — just missing.
* ``plate_index`` — the attach *reads* it to choose which plate to describe,
  so a row that arrives without one keeps NULL for ever even though the
  parser worked the answer out on its way past.
* ``extra_data["plate_id"]`` — the mirror ``queue_virtual.py`` still reads.

⚠️ ``plate_index`` is backfilled, never overwritten. The column records what
the printer was actually running, taken from live MQTT state; the 3MF only
knows what it contains. Where they disagree the live state is right.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from backend.app.models.archive import PrintArchive
from backend.app.services.archive import ArchiveService

PLATES = {
    2: {"prediction": 3600, "weight": 12.5, "bed": "textured_plate"},
    5: {"prediction": 7200, "weight": 99.0, "bed": "cool_plate"},
}


def _multi_plate_3mf(path: Path) -> Path:
    body = "".join(
        f"<plate>"
        f'<metadata key="index" value="{idx}" />'
        f'<metadata key="prediction" value="{spec["prediction"]}" />'
        f'<metadata key="weight" value="{spec["weight"]}" />'
        f'<metadata key="curr_bed_type" value="{spec["bed"]}" />'
        f"</plate>"
        for idx, spec in PLATES.items()
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/slice_info.config", f"<config>{body}</config>")
    return path


async def _attach(db_session, tmp_path, monkeypatch, printer, *, plate_index: int | None):
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    (tmp_path / "archive").mkdir(parents=True, exist_ok=True)

    archive = PrintArchive(
        printer_id=printer.id,
        filename="Plate.gcode.3mf",
        file_path="",
        file_size=0,
        print_name="Plate",
        status="printing",
        plate_index=plate_index,
    )
    db_session.add(archive)
    await db_session.commit()
    await db_session.refresh(archive)

    src = _multi_plate_3mf(tmp_path / "src.gcode.3mf")
    ok = await ArchiveService(db_session).attach_3mf_to_archive(archive.id, src, "Plate.gcode.3mf")
    assert ok, "attach failed — the assertions below would be about nothing"
    await db_session.refresh(archive)
    return archive


@pytest.mark.asyncio
@pytest.mark.integration
class TestFieldParity:
    async def test_bed_type_is_written(self, db_session, tmp_path, monkeypatch, printer_factory):
        printer = await printer_factory()
        archive = await _attach(db_session, tmp_path, monkeypatch, printer, plate_index=2)

        assert archive.bed_type == "textured_plate"

    async def test_a_missing_plate_index_is_backfilled_from_the_file(
        self, db_session, tmp_path, monkeypatch, printer_factory
    ):
        """Single-plate exports carry nothing in the gcode filename to parse,
        so the row legitimately arrives without one."""
        printer = await printer_factory()
        archive = await _attach(db_session, tmp_path, monkeypatch, printer, plate_index=None)

        assert archive.plate_index == 2, "the parser knew the plate and the column stayed NULL"

    async def test_a_known_plate_index_is_not_overwritten(self, db_session, tmp_path, monkeypatch, printer_factory):
        """⚠️ The column is what the printer is running; the container holds
        several plates and cannot arbitrate between them."""
        printer = await printer_factory()
        archive = await _attach(db_session, tmp_path, monkeypatch, printer, plate_index=5)

        assert archive.plate_index == 5
        # ...and the metadata that landed is plate 5's, not the first plate's.
        assert archive.print_time_seconds == 7200
        assert archive.filament_used_grams == pytest.approx(99.0)
        assert archive.bed_type == "cool_plate"

    async def test_extra_data_mirrors_the_plate_column(self, db_session, tmp_path, monkeypatch, printer_factory):
        printer = await printer_factory()
        archive = await _attach(db_session, tmp_path, monkeypatch, printer, plate_index=5)

        assert (archive.extra_data or {}).get("plate_id") == 5
