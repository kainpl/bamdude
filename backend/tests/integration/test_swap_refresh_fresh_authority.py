"""An API key's authority over the jobs of a refresh is read again under the refresh's locks (spec r4 §1, T9.7 R1).

The request's gates granted queue read and update and a printer scope. While the refresh waits for its locks the key
can lose any of them: its owner's queue read right, its printer list, the key itself. Read again on the writer's own
session -- enabled, unexpired, BOTH queue rights, the current printer list -- the refresh refuses before any snapshot
or audit moves. The fresh printer list only narrows what the request was granted, never widens it. Codex's acceptance
probes (CODEX-REVIEW-T9-r1, T9-A-R1) are the first three tests, kept as Codex wrote them.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.core.auth_lifetime import capture_key
from backend.app.models.api_key import APIKey
from backend.app.models.group import Group
from backend.app.models.swap_source_declaration_audit import SwapSourceDeclarationAudit
from backend.app.models.user import User
from backend.app.services import swap_job_refresh as jobs, swap_source_declarations as decl
from backend.tests.auth_helpers import bind_test_key
from backend.tests.integration.test_swap_declaration_service import (  # noqa: F401 -- fixtures
    ALL,
    a_3mf,
    a_user,
    actor_of,
    an_archive,
    clean_spool_state,
    declare,
    factory,
)
from backend.tests.integration.test_swap_job_refresh import a_job, a_printer_queue, by_id, listing, ref, row
from backend.tests.integration.test_workshop_library_rights import _user

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _a_keyed_job(factory, tmp_path, *, printer_ids):
    """A declared archive, one of its jobs on printer P, and an API key of a non-admin owner whose group grants the
    archive and queue rights; returns what a refresh needs."""
    owner = await a_user(factory)
    qid = await a_printer_queue(factory)
    path = tmp_path / "plate.gcode.3mf"
    a_3mf(path, "scope")
    aid = await an_archive(factory, path, owner=owner)
    jid = await a_job(factory, aid, queue_id=qid)
    seen = await decl.inspect("archive", aid, actor=actor_of(owner), rights=ALL)
    await declare("archive", aid, seen, owner)
    listed = await listing(aid, owner)
    async with factory() as db:
        group = Group(
            name="review-group",
            permissions=["archives:read_all", "archives:update_all", "queue:read_all", "queue:update_all"],
        )
        db.add(group)
        fresh_owner = (
            await db.execute(select(User).where(User.id == owner.id).options(selectinload(User.groups)))
        ).scalar_one()
        fresh_owner.role = "user"
        fresh_owner.groups.append(group)
        await db.flush()
        key = APIKey(
            name="review",
            key_hash="not-used",
            key_prefix="review",
            user_id=owner.id,
            enabled=True,
            printer_ids=printer_ids(qid),
            can_queue=True,
            can_read_status=True,
            can_manage_archives=True,
        )
        await bind_test_key(db, key, fresh_owner)
        db.add(key)
        await db.commit()
        owner._test_key_capture = capture_key(key)
        return owner, qid, aid, jid, listed, key.id, group.id


def _refresh(owner, aid, jid, listed, kid, scope):
    return jobs.refresh(
        "archive",
        aid,
        declaration_id=listed["declaration_id"],
        source_revision=listed["source_revision"],
        jobs=[ref(by_id(listed, "queue", jid))],
        operation_id=str(uuid.uuid4()),
        actor=decl.Actor("api_key", owner.id, "review key", kid, capture=owner._test_key_capture),
        rights=ALL,
        job_rights=jobs.JobRights(True, True),
        printer_scope=scope,
    )


async def _refresh_audits(factory) -> int:
    async with factory() as db:
        lines = (await db.execute(select(SwapSourceDeclarationAudit))).scalars().all()
        return sum(1 for line in lines if line.action == jobs.REFRESH_ACTION)


@pytest.mark.parametrize("revocation", ["printer_scope", "queue_read", "queue_update", "disabled"])
async def test_a_key_losing_authority_while_waiting_must_not_refresh(factory, tmp_path, monkeypatch, revocation):
    owner, qid, aid, jid, listed, kid, gid = await _a_keyed_job(factory, tmp_path, printer_ids=lambda qid: [qid])
    from backend.app.services import queue_ops

    original = queue_ops.claim_lanes
    waiting, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def held(*args, **kwargs):
        waiting.set()
        await asyncio.wait_for(release.wait(), 15)
        async with original(*args, **kwargs):
            yield

    monkeypatch.setattr(queue_ops, "claim_lanes", held)
    task = asyncio.create_task(_refresh(owner, aid, jid, listed, kid, frozenset({qid})))
    await asyncio.wait_for(waiting.wait(), 5)
    async with factory() as db:
        if revocation == "printer_scope":
            (await db.get(APIKey, kid)).printer_ids = []
        elif revocation == "disabled":
            (await db.get(APIKey, kid)).enabled = False
        else:
            kept = "queue:update_all" if revocation == "queue_read" else "queue:read_all"
            (await db.get(Group, gid)).permissions = ["archives:read_all", "archives:update_all", kept]
        await db.commit()
    release.set()
    with pytest.raises(HTTPException) as caught:
        await asyncio.wait_for(task, 10)
    assert caught.value.status_code == (
        401 if revocation == "disabled" else 409 if revocation == "printer_scope" else 403
    )
    assert (await row(factory, "queue", jid)).source_snapshot["swap_compatible"] is None
    assert await _refresh_audits(factory) == 0


async def test_api_key_refresh_rechecks_the_owners_queue_read_right(factory):
    async with factory() as db:
        user = await _user(db, "no_queue_read", ["queue:update_all"])
        key = APIKey(
            name="read-revoked",
            key_hash="not-used",
            key_prefix="review",
            user_id=user.id,
            enabled=True,
            can_queue=True,
            can_read_status=True,
        )
        await bind_test_key(db, key, user)
        db.add(key)
        await db.commit()
        actor = decl.Actor("api_key", user.id, "review key", key.id, capture=capture_key(key))
    async with factory() as db:
        with pytest.raises(HTTPException) as caught:
            await jobs._fresh_job_rights(db, actor, jobs.JobRights(True, True))
        assert caught.value.status_code == 403


@pytest.mark.parametrize("now", ["every_printer", "this_printer"])
async def test_a_wider_printer_list_read_later_never_widens_what_the_request_was_granted(factory, tmp_path, now):
    # The request reached the route scoped to another printer; the key now names every printer, or this one.
    printers = (lambda qid: None) if now == "every_printer" else (lambda qid: [qid])
    owner, qid, aid, jid, listed, kid, _gid = await _a_keyed_job(factory, tmp_path, printer_ids=printers)
    with pytest.raises(HTTPException) as caught:
        await _refresh(owner, aid, jid, listed, kid, frozenset({qid + 1000}))
    assert caught.value.status_code == 409
    assert caught.value.detail["jobs"] == [{"kind": "queue", "id": jid, "reason": "job_gone"}]
    assert (await row(factory, "queue", jid)).source_snapshot["swap_compatible"] is None


async def test_a_key_that_keeps_its_authority_still_refreshes(factory, tmp_path):
    owner, qid, aid, jid, listed, kid, _gid = await _a_keyed_job(factory, tmp_path, printer_ids=lambda qid: [qid])
    result = await _refresh(owner, aid, jid, listed, kid, frozenset({qid}))
    assert result["refreshed"] == [{"kind": "queue", "id": jid}]
    assert (await row(factory, "queue", jid)).source_snapshot["swap_compatible"] is True
    assert await _refresh_audits(factory) == 1


async def test_a_retry_that_waited_while_the_key_lost_the_printer_gets_no_replay(factory, tmp_path, monkeypatch):
    """The retry passed its preflight before the first request committed, then waited for the locks while the key
    lost the job's printer: under the locks the committed result is found, but not answered for a hidden job."""
    owner, qid, aid, jid, listed, kid, _gid = await _a_keyed_job(factory, tmp_path, printer_ids=lambda qid: [qid])
    from backend.app.services import queue_sources

    # Held BEFORE the exclusive storage guard, never inside it: the first request must be able to take it.
    original = queue_sources.storage_mutation
    entered, waiting, release = 0, asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def first_held(*args, **kwargs):
        nonlocal entered
        entered += 1
        if entered == 1:
            waiting.set()
            await asyncio.wait_for(release.wait(), 15)
        async with original(*args, **kwargs):
            yield

    monkeypatch.setattr(queue_sources, "storage_mutation", first_held)
    operation = str(uuid.uuid4())

    def send():
        return jobs.refresh(
            "archive",
            aid,
            declaration_id=listed["declaration_id"],
            source_revision=listed["source_revision"],
            jobs=[ref(by_id(listed, "queue", jid))],
            operation_id=operation,
            actor=decl.Actor("api_key", owner.id, "review key", kid, capture=owner._test_key_capture),
            rights=ALL,
            job_rights=jobs.JobRights(True, True),
            printer_scope=frozenset({qid}),
        )

    retry = asyncio.create_task(send())
    await asyncio.wait_for(waiting.wait(), 5)
    first = await send()  # the first request commits while the retry waits
    assert first["replayed"] is False
    async with factory() as db:
        (await db.get(APIKey, kid)).printer_ids = []
        await db.commit()
    release.set()
    with pytest.raises(HTTPException) as caught:
        await asyncio.wait_for(retry, 10)
    assert caught.value.status_code == 409
    assert caught.value.detail["jobs"] == [{"kind": "queue", "id": jid, "reason": "job_gone"}]
