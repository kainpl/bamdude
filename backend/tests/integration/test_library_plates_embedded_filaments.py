"""``/library/files/{id}/plates`` names the filament presets a project was prepared with.

The Slice dialog already defaults its printer and process to the names a 3MF
carries (#1325). A project also names a filament preset per slot — "Assemble
project" writes the one the operator chose — and the dialog can only honour it
if the plates answer carries it.
"""

import io
import json
import zipfile

import pytest

from backend.app.api.routes import library as library_routes
from backend.app.models.library import LibraryFile

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _project(settings: dict | None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("3D/3dmodel.model", "<model/>")
        if settings is not None:
            archive.writestr("Metadata/project_settings.config", json.dumps(settings))
    return buffer.getvalue()


async def _library_file(db_session, tmp_path, monkeypatch, content: bytes) -> LibraryFile:
    monkeypatch.setattr(library_routes.app_settings, "base_dir", tmp_path)
    (tmp_path / "library").mkdir()
    (tmp_path / "library" / "jar.3mf").write_bytes(content)
    row = LibraryFile(filename="jar.3mf", file_path="library/jar.3mf", file_type="3mf", file_size=len(content))
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


async def test_the_plates_answer_names_the_filament_preset_of_each_slot(
    async_client, db_session, tmp_path, monkeypatch
):
    row = await _library_file(
        db_session,
        tmp_path,
        monkeypatch,
        _project(
            {
                "printer_settings_id": "Bambu Lab X1 Carbon 0.4 nozzle",
                "print_settings_id": "0.20mm Standard @BBL X1C",
                "filament_settings_id": ["Bambu PETG HF @BBL X1C", "Bambu PLA Basic @BBL X1C"],
            }
        ),
    )

    response = await async_client.get(f"/api/v1/library/files/{row.id}/plates")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["embedded_filaments"] == ["Bambu PETG HF @BBL X1C", "Bambu PLA Basic @BBL X1C"]
    assert body["embedded_printer"] == "Bambu Lab X1 Carbon 0.4 nozzle"


async def test_a_project_that_names_no_filament_answers_an_empty_list(async_client, db_session, tmp_path, monkeypatch):
    row = await _library_file(db_session, tmp_path, monkeypatch, _project(None))

    response = await async_client.get(f"/api/v1/library/files/{row.id}/plates")

    assert response.status_code == 200, response.text
    assert response.json()["embedded_filaments"] == []
