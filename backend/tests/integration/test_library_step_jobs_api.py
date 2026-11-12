"""The STEP thumbnail jobs API (spec §5, plan A4): exactly one POST mode, keyset pages of at most min(64, Q),
receipts per actor that never name another actor, GET of your own only while you can read the file and of another's
only with read_all AND update_all, a DELETE that detaches one receipt and is too late after the hand-off, 410 for an
earlier epoch — and the legacy batch route that answers an explicit STEP id instead of losing it."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from backend.app.core.auth import create_access_token, generate_api_key
from backend.app.core.permissions import Permission
from backend.app.models.api_key import APIKey
from backend.app.models.group import Group
from backend.app.models.library import LibraryFile
from backend.app.models.user import User
from backend.app.services import library_preview_jobs as jobs_module
from backend.app.services.library_preview_jobs import QUEUE_LIMIT
from backend.tests.auth_helpers import bind_test_key, mint_test_access

pytestmark = pytest.mark.integration
URL = "/api/v1/library/thumbnail-jobs"


class Perform:
    """Admits at once; ``hold`` keeps the attempt before (or, with ``after_hand_off``, after) its hand-off."""

    def __init__(self):
        self.hold = asyncio.Event()
        self.hold.set()
        self.after_hand_off = False
        self.entered = asyncio.Event()
        self.calls = []

    async def __call__(self, source, *, order, wait_seconds, on_admit, on_hand_off, before_attach):
        self.calls.append(source.id)
        on_admit()
        if self.after_hand_off:
            on_hand_off()
        self.entered.set()
        await self.hold.wait()
        if not self.after_hand_off:
            on_hand_off()
        return "succeeded" if await before_attach() else "authority_missing"


@pytest.fixture
async def jobs():
    perform = Perform()
    service = jobs_module.start_library_preview_jobs(perform=perform)
    service.perform = perform
    try:
        yield service
    finally:
        perform.hold.set()  # a failed test never leaves a handed-off attempt for the stop to wait on


async def _file(db, n: int, *, file_type="step", owner=None, thumbnail=None, folder_id=None, trashed=False):
    row = LibraryFile(
        filename=f"part{n}.{file_type}",
        file_path=f"library/part{n}.{file_type}",
        file_type=file_type,
        file_size=100,
        file_hash=f"{n:064x}",
        thumbnail_path=thumbnail,
        folder_id=folder_id,
        created_by_id=owner.id if owner else None,
        deleted_at=datetime.now(timezone.utc) if trashed else None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _user(db, name, permissions):
    group = Group(name=f"g-{name}", description="test", permissions=[p.value for p in permissions])
    db.add(group)
    await db.flush()
    user = User(username=name, email=f"{name}@example.com", password_hash="x", role="user", is_active=True)
    user.groups.append(group)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _as(user):
    return {"Authorization": f"Bearer {await mint_test_access(username=user.username)}"}


async def _settled(client, epoch, receipt_id, headers=None, rounds=200):
    for _ in range(rounds):
        body = (await client.get(URL, params={"epoch": epoch, "ids": [receipt_id]}, headers=headers)).json()
        if body["receipts"] and body["receipts"][0]["state"] in {"succeeded", "failed", "canceled"}:
            return body["receipts"][0]
        await asyncio.sleep(0.01)
    raise AssertionError(body)


# --- POST ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_by_ids_queues_steps_refuses_other_types_and_omits_missing_or_trashed(
    async_client, db_session, jobs
):
    step = await _file(db_session, 1)
    stp = await _file(db_session, 2, file_type="stp")
    stl = await _file(db_session, 3, file_type="stl")
    trashed = await _file(db_session, 4, trashed=True)
    response = await async_client.post(URL, json={"file_ids": [step.id, stl.id, 999_999, trashed.id, stp.id, step.id]})
    assert response.status_code == 202
    body = response.json()
    assert body["jobs_epoch"] == jobs.epoch and body["next_after_id"] is None
    by_file = {item["file_id"]: item for item in body["results"]}
    assert set(by_file) == {step.id, stl.id, stp.id}  # missing and trashed: nothing about them
    assert by_file[stl.id] == {"file_id": stl.id, "receipt_id": None, "state": None, "error_code": "not_step"}
    assert by_file[step.id]["receipt_id"] and by_file[step.id]["state"] in {"queued", "running", "succeeded"}
    assert set(by_file[step.id]) == {"file_id", "receipt_id", "state", "error_code"}  # no actor, no name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"file_ids": [1], "all_missing": True},
        {"file_ids": [1], "folder_id": 3},
        {"file_ids": []},
        {"file_ids": list(range(1, 66))},
        {"all_missing": True, "limit": 65},
    ],
)
async def test_post_takes_exactly_one_mode(async_client, jobs, body):
    assert (await async_client.post(URL, json=body)).status_code == 422


@pytest.mark.asyncio
async def test_post_without_the_jobs_service_is_unavailable(async_client, db_session):
    step = await _file(db_session, 1)
    response = await async_client.post(URL, json={"file_ids": [step.id]})
    assert response.status_code == 503 and response.json()["detail"]["error"] == "unavailable"


@pytest.mark.asyncio
async def test_all_missing_is_read_as_keyset_pages_of_q_step_files_only(async_client, db_session, jobs):
    jobs.perform.hold.clear()  # nothing finishes: the pages are what is read, whatever the queue answers
    steps = [await _file(db_session, n) for n in range(1, QUEUE_LIMIT + 4)]
    await _file(db_session, 100, file_type="stl")
    await _file(db_session, 101, thumbnail="thumbs/has.png")
    first = (await async_client.post(URL, json={"all_missing": True})).json()
    assert [item["file_id"] for item in first["results"]] == [row.id for row in steps[:QUEUE_LIMIT]]
    assert first["next_after_id"] == steps[QUEUE_LIMIT - 1].id
    second = (await async_client.post(URL, json={"all_missing": True, "after_id": first["next_after_id"]})).json()
    assert [item["file_id"] for item in second["results"]] == [row.id for row in steps[QUEUE_LIMIT:]]
    assert second["next_after_id"] is None
    assert {item["error_code"] for item in second["results"]} == {"queue_full"}  # Q pending: refused, not dropped
    jobs.perform.hold.set()


@pytest.mark.asyncio
async def test_a_folder_selection_stays_in_its_folder(async_client, db_session, jobs):
    from backend.app.models.library import LibraryFolder

    folder = LibraryFolder(name="cad")
    db_session.add(folder)
    await db_session.commit()
    inside = await _file(db_session, 1, folder_id=folder.id, thumbnail="thumbs/old.png")
    await _file(db_session, 2)
    body = (await async_client.post(URL, json={"folder_id": folder.id})).json()
    assert [item["file_id"] for item in body["results"]] == [inside.id]  # a regeneration: thumbnails too


# --- GET ----------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_your_own_receipt_and_an_earlier_epoch_is_410(async_client, db_session, jobs):
    step = await _file(db_session, 1)
    item = (await async_client.post(URL, json={"file_ids": [step.id]})).json()["results"][0]
    done = await _settled(async_client, jobs.epoch, item["receipt_id"])
    assert done == {"file_id": step.id, "receipt_id": item["receipt_id"], "state": "succeeded", "error_code": None}
    old = await async_client.get(URL, params={"epoch": "0" * 32, "ids": [item["receipt_id"]]})
    assert old.status_code == 410 and old.json()["detail"]["error"] == "preview_job_expired"
    unknown = (await async_client.get(URL, params={"epoch": jobs.epoch, "ids": ["f" * 32]})).json()
    assert unknown == {"jobs_epoch": jobs.epoch, "receipts": [], "missing": ["f" * 32]}


@pytest.mark.asyncio
async def test_another_actors_receipt_needs_read_all_and_update_all_and_never_names_them(
    async_client, db_session, jobs
):
    # users first: in the tests every session shares one connection, and the runner's own read session would
    # roll back a flush still in flight here
    reader = await _user(db_session, "reader", [Permission.LIBRARY_READ_ALL])
    both = await _user(db_session, "both", [Permission.LIBRARY_READ_ALL, Permission.LIBRARY_UPDATE_ALL])
    jobs.perform.hold.clear()
    step = await _file(db_session, 1)
    receipt_id = (await async_client.post(URL, json={"file_ids": [step.id]})).json()["results"][0]["receipt_id"]
    params = {"epoch": jobs.epoch, "ids": [receipt_id]}
    hidden = (await async_client.get(URL, params=params, headers=await _as(reader))).json()
    assert hidden["receipts"] == [] and hidden["missing"] == [receipt_id]  # the same as unknown
    seen = (await async_client.get(URL, params=params, headers=await _as(both))).json()["receipts"][0]
    assert set(seen) == {"file_id", "receipt_id", "state", "error_code"}
    jobs.perform.hold.set()


@pytest.mark.asyncio
async def test_your_own_receipt_is_hidden_once_you_cannot_read_the_file(async_client, db_session, jobs):
    jobs.perform.hold.clear()
    owner = await _user(db_session, "owner", [Permission.LIBRARY_READ_ALL])
    step = await _file(db_session, 1, owner=owner)
    asker = await _user(db_session, "asker", [Permission.LIBRARY_UPDATE_ALL, Permission.LIBRARY_READ_OWN])
    receipt_id = (await async_client.post(URL, json={"file_ids": [step.id]}, headers=await _as(asker))).json()[
        "results"
    ][0]["receipt_id"]
    body = (
        await async_client.get(URL, params={"epoch": jobs.epoch, "ids": [receipt_id]}, headers=await _as(asker))
    ).json()
    assert body["missing"] == [receipt_id]  # its own receipt, but not its file to read
    jobs.perform.hold.set()


@pytest.mark.asyncio
async def test_same_account_in_another_valid_session_can_read_and_cancel_its_receipt(async_client, db_session, jobs):
    owner = await _user(db_session, "receipt-owner", [Permission.LIBRARY_UPDATE_ALL, Permission.LIBRARY_READ_OWN])
    jobs.perform.hold.clear()
    step = await _file(db_session, 1, owner=owner)
    first_headers, second_headers = await _as(owner), await _as(owner)
    assert first_headers != second_headers
    response = await async_client.post(URL, json={"file_ids": [step.id]}, headers=first_headers)
    assert response.status_code == 202, response.text
    receipt_id = response.json()["results"][0]["receipt_id"]
    original_capture = jobs._receipts[receipt_id].authority.capture

    result = await async_client.get(URL, params={"epoch": jobs.epoch, "ids": [receipt_id]}, headers=second_headers)
    assert [item["receipt_id"] for item in result.json()["receipts"]] == [receipt_id]
    canceled = await async_client.delete(f"{URL}/{receipt_id}", params={"epoch": jobs.epoch}, headers=second_headers)
    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["state"] == "canceled"
    # Reading from a second session never replaces the grant carried to the final writer.
    assert jobs._receipts[receipt_id].authority.capture is original_capture
    jobs.perform.hold.set()


@pytest.mark.asyncio
async def test_recreated_numeric_account_cannot_take_the_old_receipt(async_client, db_session, jobs):
    from sqlalchemy import delete

    owner = await _user(db_session, "old-receipt-owner", [Permission.LIBRARY_UPDATE_ALL, Permission.LIBRARY_READ_OWN])
    jobs.perform.hold.clear()
    step = await _file(db_session, 1, owner=owner)
    response = await async_client.post(URL, json={"file_ids": [step.id]}, headers=await _as(owner))
    assert response.status_code == 202, response.text
    await jobs.perform.entered.wait()  # precheck DB work has ended before replacing the fixture account
    receipt_id = response.json()["results"][0]["receipt_id"]
    old_id, old_identity, groups = owner.id, owner.account_identity, list(owner.groups)
    owner.groups.clear()
    await db_session.flush()
    await db_session.execute(delete(User).where(User.id == old_id))
    await db_session.commit()
    replacement = User(id=old_id, username="new-receipt-owner", role="user", groups=groups)
    db_session.add(replacement)
    await db_session.commit()
    assert replacement.account_identity != old_identity
    step.created_by_id = replacement.id
    await db_session.commit()
    # The reused numeric source owner allows READ_OWN; only immutable actor identity can deny the receipt.
    headers = await _as(replacement)
    result = await async_client.get(URL, params={"epoch": jobs.epoch, "ids": [receipt_id]}, headers=headers)
    assert result.json()["receipts"] == [] and result.json()["missing"] == [receipt_id]
    canceled = await async_client.delete(f"{URL}/{receipt_id}", params={"epoch": jobs.epoch}, headers=headers)
    assert canceled.status_code == 404
    jobs.perform.hold.set()


# --- DELETE -------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_detaches_only_this_receipt_and_a_foreign_one_needs_both_rights(async_client, db_session, jobs):
    other = await _user(db_session, "other", [Permission.LIBRARY_UPDATE_ALL, Permission.LIBRARY_READ_ALL])
    updater = await _user(db_session, "updater", [Permission.LIBRARY_UPDATE_ALL])
    jobs.perform.hold.clear()
    step = await _file(db_session, 1)
    mine = (await async_client.post(URL, json={"file_ids": [step.id]})).json()["results"][0]["receipt_id"]
    theirs = (await async_client.post(URL, json={"file_ids": [step.id]}, headers=await _as(other))).json()["results"][
        0
    ]["receipt_id"]
    assert theirs != mine
    refused = await async_client.delete(f"{URL}/{theirs}", params={"epoch": jobs.epoch}, headers=await _as(updater))
    assert refused.status_code == 404  # not yours, and no read_all: the same as unknown
    canceled = await async_client.delete(f"{URL}/{mine}", params={"epoch": jobs.epoch})
    assert canceled.status_code == 200 and canceled.json()["state"] == "canceled"
    jobs.perform.hold.set()
    assert (await _settled(async_client, jobs.epoch, theirs, headers=await _as(other)))["state"] == "succeeded"
    assert jobs.perform.calls == [step.id]  # one CAD for both actors


@pytest.mark.asyncio
async def test_delete_after_the_hand_off_is_too_late_and_the_terminal_follows(async_client, db_session, jobs):
    jobs.perform.hold.clear()
    jobs.perform.after_hand_off = True
    step = await _file(db_session, 1)
    receipt_id = (await async_client.post(URL, json={"file_ids": [step.id]})).json()["results"][0]["receipt_id"]
    await jobs.perform.entered.wait()
    late = await async_client.delete(f"{URL}/{receipt_id}", params={"epoch": jobs.epoch})
    assert late.status_code == 409 and late.json()["detail"]["error"] == "cancel_too_late"
    jobs.perform.hold.set()
    assert (await _settled(async_client, jobs.epoch, receipt_id))["state"] == "succeeded"
    expired = await async_client.delete(f"{URL}/{receipt_id}", params={"epoch": "0" * 32})
    assert expired.status_code == 410


@pytest.mark.asyncio
async def test_an_api_keys_receipt_is_the_keys_and_shows_no_actor(async_client, db_session, jobs):
    from sqlalchemy import select

    admin = (await db_session.execute(select(User).where(User.username == "test_admin"))).scalar_one()
    raw, key_hash, key_prefix = generate_api_key()
    key = APIKey(
        name="k", key_hash=key_hash, key_prefix=key_prefix, enabled=True, user_id=admin.id, can_manage_library=True
    )
    await bind_test_key(db_session, key, admin)
    db_session.add(key)
    await db_session.commit()
    step = await _file(db_session, 1)
    key_headers = {"X-API-Key": raw, "Authorization": ""}
    response = await async_client.post(URL, json={"file_ids": [step.id]}, headers=key_headers)
    assert response.status_code == 202
    receipt_id = response.json()["results"][0]["receipt_id"]
    receipt = jobs._receipts[receipt_id]
    assert receipt.authority.kind == "api_key" and receipt.authority.user_id is None  # never its owner's JWT


# --- the legacy batch route ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_legacy_route_answers_an_explicit_step_id_and_never_selects_step(
    async_client, db_session, monkeypatch
):
    from backend.app.api.routes import library

    async def never(*args, **kwargs):
        raise AssertionError("a STEP file reached the synchronous loop")

    monkeypatch.setattr(library, "generate_library_preview", never)
    step = await _file(db_session, 1)
    body = (await async_client.post("/api/v1/library/generate-stl-thumbnails", json={"file_ids": [step.id]})).json()
    assert body["processed"] == 1 and body["failed"] == 1
    assert body["results"][0]["error_code"] == "use_thumbnail_jobs" and body["results"][0]["file_id"] == step.id
    missing = (await async_client.post("/api/v1/library/generate-stl-thumbnails", json={"all_missing": True})).json()
    assert missing["processed"] == 0  # all_missing does not widen to STEP


@pytest.mark.asyncio
async def test_the_button_and_its_cancel_ask_update_all(async_client, db_session, jobs):
    uploader = await _user(db_session, "uploader", [Permission.LIBRARY_UPLOAD, Permission.LIBRARY_READ_ALL])
    step = await _file(db_session, 1)
    assert (await async_client.post(URL, json={"file_ids": [step.id]}, headers=await _as(uploader))).status_code == 403
    jobs.perform.hold.clear()
    receipt_id = (await async_client.post(URL, json={"file_ids": [step.id]})).json()["results"][0]["receipt_id"]
    refused = await async_client.delete(
        f"{URL}/{receipt_id}", params={"epoch": jobs.epoch}, headers=await _as(uploader)
    )
    assert refused.status_code == 403
    jobs.perform.hold.set()
