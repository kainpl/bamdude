"""Who may apply a declaration to selected jobs, over HTTP (swap-source-declaration spec r3 §6; plan T9.4).

Both doors -- the listing and the refresh -- ask the source's read and update right AND the queue's; each job is
then judged by its own owner's right, and a job the caller may not see is never listed and, named anyway, is the
same refusal as a missing one. A key limited to some printers reaches only their queues, and never the router.
The queue screens hear of a refresh only after its commit.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from backend.app.core.permissions import Permission
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.queue_source import QueueSource
from backend.app.services import queue_sources, swap_compatibility as sc
from backend.app.services.queue_source_capture import capture_staged, plan_capture
from backend.tests.integration.test_swap_declaration_service import a_3mf, sha
from backend.tests.integration.test_swap_library_ingest import clean_spool_state, sessions  # noqa: F401 -- fixtures
from backend.tests.integration.test_workshop_library_rights import _jwt, _key, _user

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

SOURCE = [
    Permission.ARCHIVES_READ_OWN.value,
    Permission.ARCHIVES_UPDATE_OWN.value,
]
QUEUE_OWN = [Permission.QUEUE_READ_OWN.value, Permission.QUEUE_UPDATE_OWN.value]
QUEUE_ALL = [Permission.QUEUE_READ_ALL.value, Permission.QUEUE_UPDATE_ALL.value]


async def a_queue(db, printer_factory) -> int:
    printer = await printer_factory()
    if await db.get(PrinterQueue, printer.id) is None:
        db.add(PrinterQueue(id=printer.id, printer_id=printer.id, status="idle"))
        await db.commit()
    return printer.id


async def an_archive(db, tmp_path, owner) -> int:
    path = tmp_path / f"{uuid.uuid4().hex}.gcode.3mf"
    raw = a_3mf(path, path.name)
    archive = PrintArchive(
        filename="plate.gcode.3mf",
        file_path=str(path),
        file_size=len(raw),
        status="completed",
        source_content_hash=sha(raw),
        content_hash=sha(raw),
        created_by_id=owner.id,
    )
    db.add(archive)
    await db.commit()
    return archive.id


async def a_job(sessions, db, archive_id: int, *, queue_id=None, kind="queue", owner=None) -> int:  # noqa: F811
    staged = await capture_staged(plan_capture(archive=await db.get(PrintArchive, archive_id)))
    made: list[int] = []

    async def attach(session, source: QueueSource) -> None:
        snapshot = sc.snapshot_with_fact(queue_sources.snapshot_for(staged.receipt, source), sc.UNKNOWN_SOURCE)
        columns = {
            "status": "failed",
            "archive_id": archive_id,
            "queue_source_id": source.id,
            "source_snapshot": snapshot,
            "created_by_id": owner.id if owner else None,
        }
        row = PrintQueueItem(queue_id=queue_id, position=1, **columns) if kind == "queue" else AutoQueueItem(**columns)
        session.add(row)
        await session.flush()
        made.append(row.id)

    await queue_sources.publish(staged.receipt, attach, session_factory=sessions)
    return made[0]


async def declared(client, archive_id, headers, value=True):
    seen = (await client.post(f"/api/v1/archives/{archive_id}/swap-declaration/inspect", headers=headers)).json()
    done = await client.put(
        f"/api/v1/archives/{archive_id}/swap-declaration",
        json={
            "value": value,
            "expected_sha256": seen["sha256"],
            "expected_revision": seen["revision"],
            "operation_id": str(uuid.uuid4()),
        },
        headers=headers,
    )
    assert done.status_code == 200, done.text


async def listed(client, archive_id, headers):
    return await client.get(f"/api/v1/archives/{archive_id}/swap-declaration/jobs", headers=headers)


async def refreshed(client, archive_id, listing, picked, headers):
    return await client.post(
        f"/api/v1/archives/{archive_id}/swap-declaration/jobs/refresh",
        json={
            "declaration_id": listing["declaration_id"],
            "source_revision": listing["source_revision"],
            "operation_id": str(uuid.uuid4()),
            "jobs": picked,
        },
        headers=headers,
    )


def pick(listing, kind, job_id):
    entry = next(e for e in listing["jobs"] if (e["kind"], e["id"]) == (kind, job_id))
    return {"kind": kind, "id": job_id, "revision": entry["revision"]}


@pytest.fixture
def notices(monkeypatch):
    from backend.app.core.websocket import ws_manager

    sent: list[int] = []

    async def queue_changed(printer_id: int) -> None:
        sent.append(printer_id)

    monkeypatch.setattr(ws_manager, "send_queue_changed", queue_changed)
    return sent


async def test_the_round_trip_over_http_and_one_notice_after_the_commit(
    committing_client, db_session, sessions, tmp_path, printer_factory, notices
):
    owner = await _user(db_session, "rf_owner", SOURCE + QUEUE_OWN)
    queue_id = await a_queue(db_session, printer_factory)
    archive_id = await an_archive(db_session, tmp_path, owner)
    job_id = await a_job(sessions, db_session, archive_id, queue_id=queue_id, owner=owner)
    await declared(committing_client, archive_id, await _jwt("rf_owner"))

    listing = (await listed(committing_client, archive_id, await _jwt("rf_owner"))).json()
    assert listing["counts"] == {"eligible": 1, "refused": 0}
    done = await refreshed(
        committing_client, archive_id, listing, [pick(listing, "queue", job_id)], await _jwt("rf_owner")
    )

    assert done.status_code == 200, done.text
    assert done.json()["refreshed"] == [{"kind": "queue", "id": job_id}]
    item = await db_session.get(PrintQueueItem, job_id, populate_existing=True)
    assert sc.snapshot_fact(item.source_snapshot) == sc.SourceFact(True) and item.status == "failed"
    assert notices == [queue_id]


async def test_a_refused_refresh_sends_no_notice(
    committing_client, db_session, sessions, tmp_path, printer_factory, notices
):
    owner = await _user(db_session, "rf_refused", SOURCE + QUEUE_OWN)
    queue_id = await a_queue(db_session, printer_factory)
    archive_id = await an_archive(db_session, tmp_path, owner)
    job_id = await a_job(sessions, db_session, archive_id, queue_id=queue_id, owner=owner)
    await declared(committing_client, archive_id, await _jwt("rf_refused"))
    listing = (await listed(committing_client, archive_id, await _jwt("rf_refused"))).json()
    stale = {**pick(listing, "queue", job_id), "revision": "0" * 64}

    answer = await refreshed(committing_client, archive_id, listing, [stale], await _jwt("rf_refused"))

    assert answer.status_code == 409 and answer.json()["detail"]["error"] == "jobs_changed"
    assert notices == []


@pytest.mark.parametrize("permissions", [SOURCE, SOURCE + [Permission.QUEUE_READ_OWN.value]])
async def test_both_doors_ask_the_queue_rights_too(permissions, committing_client, db_session, tmp_path):
    owner = await _user(db_session, f"rf_noq_{len(permissions)}", permissions)
    archive_id = await an_archive(db_session, tmp_path, owner)

    answer = await listed(committing_client, archive_id, await _jwt(owner.username))

    assert answer.status_code == 403


async def test_another_owners_job_is_neither_listed_nor_refreshed(
    committing_client, db_session, sessions, tmp_path, printer_factory
):
    owner = await _user(db_session, "rf_mine", SOURCE + QUEUE_OWN)
    other = await _user(db_session, "rf_theirs", QUEUE_ALL)
    queue_id = await a_queue(db_session, printer_factory)
    archive_id = await an_archive(db_session, tmp_path, owner)
    mine = await a_job(sessions, db_session, archive_id, queue_id=queue_id, owner=owner)
    theirs = await a_job(sessions, db_session, archive_id, queue_id=queue_id, owner=other)
    await declared(committing_client, archive_id, await _jwt("rf_mine"))

    listing = (await listed(committing_client, archive_id, await _jwt("rf_mine"))).json()
    assert [(e["kind"], e["id"]) for e in listing["jobs"]] == [("queue", mine)]
    their_revision = (await listed(committing_client, archive_id, await _jwt("rf_mine"))).json()  # never theirs
    assert all(e["id"] != theirs for e in their_revision["jobs"])
    answer = await refreshed(
        committing_client,
        archive_id,
        listing,
        [pick(listing, "queue", mine), {"kind": "queue", "id": theirs, "revision": "a" * 64}],
        await _jwt("rf_mine"),
    )

    assert answer.status_code == 409
    assert answer.json()["detail"]["jobs"] == [{"kind": "queue", "id": theirs, "reason": "job_gone"}]
    item = await db_session.get(PrintQueueItem, mine, populate_existing=True)
    assert sc.snapshot_fact(item.source_snapshot).value is None


async def test_a_key_limited_to_some_printers_reaches_only_their_queues(
    committing_client, db_session, sessions, tmp_path, printer_factory
):
    owner = await _user(
        db_session,
        "rf_keyowner",
        [Permission.ARCHIVES_READ_ALL.value, Permission.ARCHIVES_UPDATE_ALL.value] + QUEUE_ALL,
    )
    allowed = await a_queue(db_session, printer_factory)
    elsewhere = await a_queue(db_session, printer_factory)
    archive_id = await an_archive(db_session, tmp_path, owner)
    inside = await a_job(sessions, db_session, archive_id, queue_id=allowed, owner=owner)
    outside = await a_job(sessions, db_session, archive_id, queue_id=elsewhere, owner=owner)
    auto = await a_job(sessions, db_session, archive_id, kind="auto_queue", owner=owner)
    await declared(committing_client, archive_id, await _jwt("rf_keyowner"))
    key = {"X-API-Key": await _key(db_session, owner, printer_ids=[allowed])}
    committing_client.headers.pop("Authorization", None)

    listing = (await listed(committing_client, archive_id, key)).json()
    assert [(e["kind"], e["id"]) for e in listing["jobs"]] == [("queue", inside)]
    answer = await refreshed(
        committing_client,
        archive_id,
        listing,
        [
            pick(listing, "queue", inside),
            {"kind": "queue", "id": outside, "revision": "a" * 64},
            {"kind": "auto_queue", "id": auto, "revision": "a" * 64},
        ],
        key,
    )

    assert answer.status_code == 409
    assert sorted((j["kind"], j["id"], j["reason"]) for j in answer.json()["detail"]["jobs"]) == [
        ("auto_queue", auto, "job_gone"),
        ("queue", outside, "job_gone"),
    ]


@pytest.mark.parametrize(
    "jobs",
    [
        [],
        [{"kind": "batch", "id": 1, "revision": "a" * 64}],
        [{"kind": "queue", "id": "1", "revision": "a" * 64}],
        [{"kind": "queue", "id": n, "revision": "a" * 64} for n in range(1, 102)],
    ],
)
async def test_the_selection_is_validated_before_anything_is_read(jobs, committing_client, db_session, tmp_path):
    owner = await _user(db_session, f"rf_shape_{len(jobs)}", SOURCE + QUEUE_OWN)
    archive_id = await an_archive(db_session, tmp_path, owner)

    answer = await committing_client.post(
        f"/api/v1/archives/{archive_id}/swap-declaration/jobs/refresh",
        json={
            "declaration_id": str(uuid.uuid4()),
            "source_revision": "a" * 64,
            "operation_id": str(uuid.uuid4()),
            "jobs": jobs,
        },
        headers=await _jwt(owner.username),
    )

    assert answer.status_code == 422


async def test_a_media_token_opens_neither_door(committing_client, db_session, tmp_path):
    owner = await _user(db_session, "rf_media", SOURCE + QUEUE_OWN)
    archive_id = await an_archive(db_session, tmp_path, owner)
    token = (await committing_client.post("/api/v1/auth/media-token", headers=await _jwt("rf_media"))).json()["token"]
    committing_client.headers.pop("Authorization", None)

    answer = await committing_client.get(f"/api/v1/archives/{archive_id}/swap-declaration/jobs?token={token}")

    assert answer.status_code == 401
    assert (await db_session.execute(select(PrintQueueItem))).scalars().all() == []


async def test_queue_rights_revoked_after_the_listing_commit_nothing(
    committing_client, db_session, sessions, tmp_path, printer_factory
):
    from backend.app.models.group import Group

    owner = await _user(db_session, "rf_revoked", SOURCE + QUEUE_OWN)
    queue_id = await a_queue(db_session, printer_factory)
    archive_id = await an_archive(db_session, tmp_path, owner)
    job_id = await a_job(sessions, db_session, archive_id, queue_id=queue_id, owner=owner)
    await declared(committing_client, archive_id, await _jwt("rf_revoked"))
    listing = (await listed(committing_client, archive_id, await _jwt("rf_revoked"))).json()
    real_lock = queue_sources.storage_mutation

    def revoke_then_lock():
        async def revoke():
            group = (await db_session.execute(select(Group).where(Group.name == "grp-rf_revoked"))).scalar_one()
            group.permissions = SOURCE + [Permission.QUEUE_READ_OWN.value]  # update_own is gone
            await db_session.commit()

        return _Then(revoke(), real_lock())

    import backend.app.services.queue_sources as spool

    spool_patch = pytest.MonkeyPatch()
    spool_patch.setattr(spool, "storage_mutation", revoke_then_lock)
    try:
        answer = await refreshed(
            committing_client, archive_id, listing, [pick(listing, "queue", job_id)], await _jwt("rf_revoked")
        )
    finally:
        spool_patch.undo()

    assert answer.status_code == 403, answer.text
    item = await db_session.get(PrintQueueItem, job_id, populate_existing=True)
    assert sc.snapshot_fact(item.source_snapshot).value is None


class _Then:
    """An async context manager that first awaits ``before``, then enters ``inner``."""

    def __init__(self, before, inner):
        self.before, self.inner = before, inner

    async def __aenter__(self):
        await self.before
        return await self.inner.__aenter__()

    async def __aexit__(self, *exc):
        return await self.inner.__aexit__(*exc)
