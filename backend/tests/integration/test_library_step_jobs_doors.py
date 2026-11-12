"""The doors a new STEP file comes in by (spec §4.1, §5; plan A4): HTTP upload, ZIP, Telegram and product import
commit the file first and then ask for its picture without waiting for CAD, each with an explicit authority
descriptor carried from the door — a missing one is authority_missing, never the system. A deduplicated or
restored file, a scan and a disabled flag ask for nothing; a ZIP asks only after its own STL/OBJ pictures."""

from __future__ import annotations

import asyncio
import io
import logging
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.api.routes import library
from backend.app.services import library_preview_jobs as jobs_module
from backend.app.services.library_preview_jobs import Authority
from backend.tests.auth_helpers import bind_test_key, mint_test_authority

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
BOX = (ROOT / "backend" / "tests" / "fixtures" / "step" / "authored" / "box_mm.step").read_bytes()


class Hold:
    """A perform that never finishes by itself: whatever a door answers, it answered before any CAD ended."""

    def __init__(self):
        self.release = asyncio.Event()
        self.calls = []

    async def __call__(self, source, *, order, wait_seconds, on_admit, on_hand_off, before_attach):
        self.calls.append(source.id)
        on_admit()
        await self.release.wait()
        on_hand_off()
        return "succeeded"


@pytest.fixture
async def jobs():
    perform = Hold()
    service = jobs_module.start_library_preview_jobs(perform=perform)
    service.perform = perform
    try:
        yield service
    finally:
        perform.release.set()


def _stl() -> bytes:
    import trimesh

    return trimesh.creation.box().export(file_type="stl")


# --- HTTP upload --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_uploaded_step_is_committed_and_queued_without_waiting(async_client, db_session, jobs):
    response = await async_client.post(
        "/api/v1/library/files", files={"file": ("bracket.step", BOX, "application/octet-stream")}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "created" and body["thumbnail_path"] is None
    ticket = body["thumbnail_job"]
    assert ticket["jobs_epoch"] == jobs.epoch and ticket["state"] in {"queued", "running"}
    receipt = jobs._receipts[ticket["receipt_id"]]
    assert (receipt.purpose, receipt.authority.kind) == ("auto", "user")
    assert jobs.perform.release.is_set() is False  # the answer came while the job still holds


@pytest.mark.asyncio
async def test_no_job_when_the_flag_is_off_for_an_stl_or_for_bytes_already_here(async_client, db_session, jobs):
    off = await async_client.post(
        "/api/v1/library/files",
        params={"generate_stl_thumbnails": "false"},
        files={"file": ("off.step", BOX + b"\n", "application/octet-stream")},
    )
    assert off.status_code == 200 and off.json()["thumbnail_job"] is None
    stl = await async_client.post("/api/v1/library/files", files={"file": ("cube.stl", _stl(), "model/stl")})
    assert stl.json()["thumbnail_job"] is None
    first = await async_client.post(
        "/api/v1/library/files", files={"file": ("a.step", BOX, "application/octet-stream")}
    )
    assert first.json()["thumbnail_job"]["receipt_id"]
    again = await async_client.post(
        "/api/v1/library/files", files={"file": ("b.step", BOX, "application/octet-stream")}
    )
    assert again.json()["outcome"] == "deduped" and again.json()["thumbnail_job"] is None  # never for a dedup
    assert len(jobs._receipts) == 1


@pytest.mark.asyncio
async def test_an_upload_still_succeeds_when_the_jobs_cannot_take_it(async_client, db_session, caplog):
    caplog.set_level(logging.WARNING, logger="backend.app.api.routes.library")
    response = await async_client.post(
        "/api/v1/library/files", files={"file": ("lonely.step", BOX, "application/octet-stream")}
    )
    assert response.status_code == 200 and response.json()["outcome"] == "created"
    assert response.json()["thumbnail_job"] == {
        "jobs_epoch": None,
        "receipt_id": None,
        "state": None,
        "error_code": "unavailable",
    }
    assert "STEP thumbnail not queued" in caplog.text


@pytest.mark.asyncio
async def test_an_api_key_upload_carries_the_key_as_its_authority(async_client, db_session, jobs):
    from sqlalchemy import select

    from backend.app.core.auth import generate_api_key
    from backend.app.models.api_key import APIKey
    from backend.app.models.user import User

    admin = (await db_session.execute(select(User).where(User.username == "test_admin"))).scalar_one()
    raw, key_hash, key_prefix = generate_api_key()
    key = APIKey(
        name="k", key_hash=key_hash, key_prefix=key_prefix, enabled=True, user_id=admin.id, can_manage_library=True
    )
    await bind_test_key(db_session, key, admin)
    db_session.add(key)
    await db_session.commit()
    response = await async_client.post(
        "/api/v1/library/files",
        files={"file": ("keyed.step", BOX, "application/octet-stream")},
        headers={"X-API-Key": raw, "Authorization": ""},
    )
    assert response.status_code == 200
    receipt = jobs._receipts[response.json()["thumbnail_job"]["receipt_id"]]
    assert receipt.authority.kind == "api_key"


# --- ZIP ----------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_zip_queues_its_steps_only_after_its_own_stl_pictures(async_client, db_session, jobs, monkeypatch):
    events, budgets = [], []

    async def stl_preview(db, source, *, pause_budget=None):
        events.append(("stl", source.filename))
        budgets.append(pause_budget)
        return False

    real_enqueue = jobs.enqueue

    def enqueue(source, authority, purpose):
        events.append(("step", source.filename))
        return real_enqueue(source, authority, purpose)

    monkeypatch.setattr(library, "generate_library_preview", stl_preview)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("one.step", BOX)
        archive.writestr("cube.stl", _stl())
        archive.writestr("two.stp", BOX + b"\n")
    response = await async_client.post(
        "/api/v1/library/files/extract-zip", files={"file": ("bundle.zip", content.getvalue(), "application/zip")}
    )
    assert response.status_code == 200
    assert [kind for kind, _ in events] == ["stl", "step", "step"]  # STEP only after the synchronous STL/OBJ
    assert len(budgets) == 1 and budgets[0] is not None  # the ZIP's one bound on a backup's pause
    by_name = {item["filename"]: item for item in response.json()["files"]}
    assert by_name["one.step"]["thumbnail_job"]["receipt_id"] and by_name["two.stp"]["thumbnail_job"]["receipt_id"]
    assert by_name["cube.stl"]["thumbnail_job"] is None


# --- shared upload: Telegram and product import -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_door_without_a_descriptor_saves_the_file_but_asks_for_nothing(db_session, jobs, caplog):
    caplog.set_level(logging.WARNING, logger="backend.app.api.routes.library")
    result = await library.store_library_upload(db_session, filename="bare.step", content=BOX, target_folder=None)
    assert result.outcome == "created"
    assert result.thumbnail_job["error_code"] == "authority_missing" and not jobs._receipts
    assert "authority_missing" in caplog.text


@pytest.mark.asyncio
async def test_the_telegram_scene_passes_its_chat_as_the_authority(db_session, jobs, monkeypatch):
    from backend.app.models.notification import NotificationProvider
    from backend.app.models.telegram_chat import TelegramChat
    from backend.app.services.telegram_handlers import library_upload_scene as scene

    provider = NotificationProvider(name="bot", provider_type="telegram", enabled=True, config='{"bot_token": "1:x"}')
    db_session.add(provider)
    await db_session.flush()
    from backend.app.models.group import Group

    group = Group(name="upload-chat", permissions=["library:upload"])
    db_session.add(group)
    await db_session.flush()
    chat = TelegramChat(chat_id=777, provider_id=provider.id, is_active=True, group_id=group.id)
    db_session.add(chat)
    await db_session.commit()
    await db_session.refresh(chat)
    monkeypatch.setattr(scene, "get_language", AsyncMock(return_value="en"))

    async def download(file_id, destination):
        destination.write(BOX)

    callback = SimpleNamespace(
        data="libup:folder:0",
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock()),
        bot=SimpleNamespace(download=download),
    )
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"file_id": "tg1", "file_name": "tg.step"}), clear=AsyncMock()
    )
    await scene.cb_folder_chosen(callback, state, tg_chat=chat)
    (receipt,) = jobs._receipts.values()
    assert receipt.authority == Authority.telegram_chat(chat.id, provider.id, 777) and receipt.purpose == "auto"


@pytest.mark.asyncio
async def test_a_product_import_passes_the_importer_as_the_authority(db_session, jobs):
    from backend.app.models.user import User
    from backend.app.services.product_card import _ingest_into_library

    user = User(username="product-importer", role="admin")
    db_session.add(user)
    await db_session.commit()
    authority = Authority.user(user.id, capture=await mint_test_authority(db_session, username=user.username))
    result = await _ingest_into_library(
        db_session,
        filename="imported.step",
        content=BOX,
        target_folder=None,
        user=user,
        authority=authority,
    )
    assert result.outcome == "created"
    (receipt,) = jobs._receipts.values()
    assert receipt.authority == authority


# --- what never asks ----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_scan_never_queues_a_step(async_client, db_session, jobs, tmp_path):
    from backend.tests.integration.test_external_folders_api import scan_and_wait

    share = tmp_path / "share"
    share.mkdir()
    (share / "found.step").write_bytes(BOX)
    created = await async_client.post(
        "/api/v1/library/folders/external", json={"name": "Share", "external_path": str(share), "readonly": True}
    )
    assert created.status_code in {200, 201}, created.text
    await scan_and_wait(async_client, created.json()["id"])
    from sqlalchemy import select

    from backend.app.models.library import LibraryFile

    found = (await db_session.execute(select(LibraryFile).where(LibraryFile.filename == "found.step"))).scalar_one()
    assert found.file_type == "step"  # the scan did see it
    assert not jobs._receipts  # the scanner never generates CAD; the explicit button does


@pytest.mark.asyncio
async def test_a_zip_asks_for_a_bounded_number_of_step_pictures(async_client, db_session, jobs, monkeypatch):
    monkeypatch.setattr(jobs_module, "REQUEST_LIMIT", 1)
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("one.step", BOX)
        archive.writestr("two.step", BOX + b"\n")
    response = await async_client.post(
        "/api/v1/library/files/extract-zip", files={"file": ("many.zip", content.getvalue(), "application/zip")}
    )
    by_name = {item["filename"]: item["thumbnail_job"] for item in response.json()["files"]}
    assert by_name["one.step"]["receipt_id"] and by_name["two.step"]["error_code"] == "queue_full"
    assert len(jobs._receipts) == 1  # the rest is refused, never queued unbounded


@pytest.mark.asyncio
async def test_a_restored_file_asks_for_nothing(async_client, db_session, jobs):
    from backend.app.api.routes.library import to_absolute_path
    from backend.app.models.library import LibraryFile

    first = await async_client.post(
        "/api/v1/library/files", files={"file": ("a.step", BOX, "application/octet-stream")}
    )
    row = await db_session.get(LibraryFile, first.json()["id"])
    to_absolute_path(row.file_path).unlink()  # the row lost its bytes
    again = await async_client.post(
        "/api/v1/library/files", files={"file": ("b.step", BOX, "application/octet-stream")}
    )
    assert again.json()["outcome"] == "restored" and again.json()["thumbnail_job"] is None
    assert len(jobs._receipts) == 1  # only the first upload's
