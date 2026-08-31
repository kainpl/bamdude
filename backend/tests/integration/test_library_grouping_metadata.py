"""Grouping metadata for a whole selection, from the DB and nothing else.

The sequencer needs to know, for every file the operator picked, which plates
it has and what filament TYPES each plate needs — before it opens any dialog.
``/library/files/{id}/plates`` answers that per file by opening the 3MF; asking
it 60 times is the cost this endpoint removes.
"""

import json

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _file(db_session, filename: str, metadata: dict | None):
    from backend.app.models.library import LibraryFile

    row = LibraryFile(
        filename=filename,
        file_path=f"library/{filename}",
        file_size=1024,
        file_type="gcode.3mf",
        file_metadata=metadata,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


SLICED = {
    "sliced_for_model": "P1S",
    "nozzle_diameter": 0.6,
    "bed_type": "Textured PEI Plate",
    "plates": [
        {"index": 1, "filaments": [{"slot_id": 1, "type": "PETG", "color": "#FF0000"}]},
        {
            "index": 2,
            "filaments": [
                {"slot_id": 1, "type": "PETG", "color": "#00FF00"},
                {"slot_id": 3, "type": "PLA", "color": "#000000"},
            ],
        },
    ],
}


async def test_it_returns_one_row_per_file_with_its_plates(async_client, db_session):
    f = await _file(db_session, "badges.gcode.3mf", SLICED)

    resp = await async_client.get(f"/api/v1/library/grouping-metadata?ids={f.id}")

    assert resp.status_code == 200, resp.text
    (row,) = resp.json()
    assert row["file_id"] == f.id
    assert row["sliced_for_model"] == "P1S"
    assert row["nozzle_diameter"] == 0.6
    assert row["bed_type"] == "Textured PEI Plate"
    assert [p["index"] for p in row["plates"]] == [1, 2]


async def test_a_plate_reports_its_filament_types_not_its_colours(async_client, db_session):
    """⚠️ Colour is deliberately absent from the payload. It is not part of any
    grouping key, and shipping it would invite a caller to key on it."""
    f = await _file(db_session, "badges.gcode.3mf", SLICED)

    resp = await async_client.get(f"/api/v1/library/grouping-metadata?ids={f.id}")

    plates = resp.json()[0]["plates"]
    assert plates[0]["filament_types"] == ["PETG"]
    assert plates[1]["filament_types"] == ["PETG", "PLA"]
    assert "color" not in json.dumps(plates)


async def test_a_file_with_no_plate_metadata_comes_back_with_no_plates(async_client, db_session):
    """It must still appear in the answer. Omitting it would leave the caller
    unable to tell "not queueable" from "id I never asked about"."""
    f = await _file(db_session, "raw.stl", None)

    resp = await async_client.get(f"/api/v1/library/grouping-metadata?ids={f.id}")

    (row,) = resp.json()
    assert row["file_id"] == f.id
    assert row["plates"] == []


async def test_unknown_ids_are_skipped_rather_than_erroring(async_client, db_session):
    f = await _file(db_session, "badges.gcode.3mf", SLICED)

    resp = await async_client.get(f"/api/v1/library/grouping-metadata?ids={f.id},999999")

    assert resp.status_code == 200
    assert [r["file_id"] for r in resp.json()] == [f.id]
