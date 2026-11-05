"""The connector's raw upload (OrcaBamDude spec r2 §6).

- The bytes go through the library's own ingest: a new file is created, the same bytes again are deduplicated.
- The cap holds before the body when Content-Length says so and while streaming when it does not; the staging file
  is ours and goes in every case, the library is never touched by an overflow.
- Two uploads per process: the third is 429 with Retry-After before its body is read.
- A row the key cannot see after ingest is a 409 with exactly ``error`` and ``message`` -- no id, no name.
- The upload queues nothing, and its answer is what a submission then names.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import pytest
from sqlalchemy import func, select, update

from backend.app.core.auth import generate_api_key
from backend.app.models.api_key import APIKey
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.settings import Settings
from backend.app.services import queue_sources
from backend.app.services.connector_runtime import connector_runtime
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf
from backend.tests.integration.test_filament_routing_dispatch import setup_source

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

BASE = "/api/v1/connector"


STAGING = {}


@pytest.fixture(autouse=True)
def _clean_state(tmp_path, monkeypatch):
    from backend.app.api.routes import connector as routes

    queue_sources._reset_state()
    connector_runtime._reset_for_tests()
    staging = tmp_path / "connector-uploads"
    staging.mkdir()
    STAGING["dir"] = staging
    monkeypatch.setattr(routes, "_staging_dir", lambda: staging)
    yield
    queue_sources._reset_state()
    connector_runtime._reset_for_tests()


@pytest.fixture
async def key(db_session):
    raw, key_hash, prefix = generate_api_key()
    row = APIKey(
        name="orca",
        key_hash=key_hash,
        key_prefix=prefix,
        enabled=True,
        can_queue=True,
        can_read_status=True,
        can_manage_library=True,
    )
    db_session.add(row)
    await db_session.commit()
    return raw


@pytest.fixture
def model(tmp_path) -> bytes:
    path = write_routing_3mf(
        tmp_path / "upload.gcode.3mf", {15: [{"id": 3, "type": "PLA", "color": "#FF0000", "used_g": "0.0001"}]}
    )
    return path.read_bytes()


def _staged() -> list:
    return list(STAGING["dir"].iterdir())


async def _upload(client, raw, content, name="part.gcode.3mf", **headers):
    return await client.post(
        f"{BASE}/files",
        params={"filename": name},
        content=content,
        headers={"X-API-Key": raw, "Content-Type": "application/octet-stream", **headers},
    )


async def test_new_bytes_create_a_file_and_the_same_bytes_are_deduplicated(async_client, db_session, key, model):
    first = await _upload(async_client, key, model)
    again = await _upload(async_client, key, model, name="renamed.gcode.3mf")

    assert first.status_code == 200, first.text
    assert first.json()["outcome"] == "created"
    assert first.json()["file_hash"] == hashlib.sha256(model).hexdigest()
    assert again.json()["outcome"] == "deduped" and again.json()["id"] == first.json()["id"]
    assert await db_session.scalar(select(func.count()).select_from(LibraryFile)) == 1
    assert await db_session.scalar(select(func.count()).select_from(PrintQueueItem)) == 0
    assert _staged() == []


async def test_a_declared_length_over_the_cap_is_refused_before_the_body(async_client, db_session, key, model):
    db_session.add(Settings(key="connector_max_upload_bytes", value="100"))
    await db_session.commit()

    response = await _upload(async_client, key, model)

    assert response.status_code == 413 and response.json()["detail"]["error"] == "upload_too_large"
    assert await db_session.scalar(select(func.count()).select_from(LibraryFile)) == 0


async def test_a_stream_over_the_cap_is_cut_and_its_staging_removed(async_client, db_session, key, model):
    db_session.add(Settings(key="connector_max_upload_bytes", value="100"))
    await db_session.commit()

    async def chunks():
        for start in range(0, len(model), 256):
            yield model[start : start + 256]

    response = await async_client.post(
        f"{BASE}/files",
        params={"filename": "part.gcode.3mf"},
        content=chunks(),
        headers={"X-API-Key": key, "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 413 and response.json()["detail"]["error"] == "upload_too_large"
    assert await db_session.scalar(select(func.count()).select_from(LibraryFile)) == 0
    assert _staged() == []


async def test_only_a_3mf_is_accepted(async_client, key):
    response = await _upload(async_client, key, b"G28\n", name="part.gcode")

    assert response.status_code == 422 and response.json()["detail"]["error"] == "invalid_file"


async def test_a_third_upload_is_refused_before_its_body(async_client, key, model):
    gate = asyncio.Event()
    busy = [asyncio.create_task(gate.wait()) for _ in range(2)]
    connector_runtime._uploads.update(busy)
    try:
        response = await _upload(async_client, key, model)
    finally:
        gate.set()
        await asyncio.gather(*busy)

    assert response.status_code == 429
    assert response.json()["detail"]["error"] == "capacity_exceeded"
    assert response.headers.get("retry-after")


async def test_a_row_the_key_cannot_see_is_a_minimal_conflict(async_client, db_session, key, model, monkeypatch):
    from backend.app.api.routes import library

    real = library.store_library_upload

    async def then_trashed(db, **kwargs):
        result = await real(db, **kwargs)
        await db.execute(update(LibraryFile).where(LibraryFile.id == result.file.id).values(deleted_at=func.now()))
        await db.commit()
        return result

    monkeypatch.setattr(library, "store_library_upload", then_trashed)

    response = await _upload(async_client, key, model)

    assert response.status_code == 409
    assert response.json() == {
        "detail": {"error": "exists_not_visible", "message": "The uploaded content is not accessible to this API key."}
    }


async def test_a_key_without_library_rights_cannot_upload(async_client, db_session, model):
    raw, key_hash, prefix = generate_api_key()
    db_session.add(APIKey(name="ro", key_hash=key_hash, key_prefix=prefix, enabled=True, can_manage_library=False))
    await db_session.commit()

    response = await _upload(async_client, raw, model)

    assert response.status_code == 403
    assert await db_session.scalar(select(func.count()).select_from(LibraryFile)) == 0


async def test_the_uploaded_file_is_what_a_submission_then_prints(
    async_client, db_session, key, model, tmp_path, printer_factory, monkeypatch
):
    _source, printer, _queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    uploaded = (await _upload(async_client, key, model)).json()
    caps = (await async_client.get(f"{BASE}/capabilities", headers={"X-API-Key": key})).json()
    body = {
        "protocol_version": 1,
        "operation_id": str(uuid.uuid4()),
        "server_id": caps["connector_server_id"],
        "storage_epoch": caps["storage_epoch"],
        "library_file_id": uploaded["id"],
        "source_sha256": uploaded["file_hash"],
        "entries": [
            {
                "entry_id": str(uuid.uuid4()),
                "plate_id": 15,
                "target": {"kind": "printer", "printer_id": printer.id, "printer_identity": printer.connector_identity},
                "action": "queue_asap",
            }
        ],
    }

    response = await async_client.post(f"{BASE}/submissions", json=body, headers={"X-API-Key": key})

    assert response.status_code == 201, response.text
    assert response.json()["state"] == "complete"
    assert await db_session.scalar(select(func.count()).select_from(PrintQueueItem)) == 1
