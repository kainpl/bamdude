"""An API key declares and refreshes on a file SQLite database without stalling the farm (T9 final review, C1).

The rights are read again inside the write transaction (spec r3 §4). For an API key that must not mean validating
the key afresh through ``resolve_api_key_authority``: that opens its own session and writes the key's ``last_used``,
which on SQLite waits for the very writer this transaction holds -- fifteen seconds of a stalled farm, then a false
"no longer allowed". The key's row is read again on the same session instead, writing nothing.
"""

from __future__ import annotations

import time
import uuid

import pytest
from starlette.requests import Request

from backend.app.core.auth import generate_api_key
from backend.app.models.api_key import APIKey
from backend.app.services import swap_job_refresh as jobs, swap_source_declarations as decl
from backend.tests.auth_helpers import bind_test_key
from backend.tests.integration.test_swap_declaration_service import (  # noqa: F401 -- fixtures
    ALL,
    a_3mf,
    a_user,
    actor_of,
    an_archive,
    clean_spool_state,
    factory,
)
from backend.tests.integration.test_swap_job_refresh import QUEUE_ALL, a_job, a_printer_queue, by_id, ref

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

#: Well under SQLite's 15 s busy timeout.
QUICK = 8


@pytest.fixture(autouse=True)
def keys_on_this_database(factory, monkeypatch):
    """The key's validation opens its own sessions, by its own module's name for them: the same file database."""
    monkeypatch.setattr("backend.app.core.auth.async_session", factory)


async def a_key(factory, owner, **flags) -> str:
    raw, key_hash, key_prefix = generate_api_key()
    async with factory() as db:
        db.add(
            await bind_test_key(
                db,
                APIKey(
                    name=f"k-{key_prefix}",
                    key_hash=key_hash,
                    key_prefix=key_prefix,
                    enabled=True,
                    user_id=owner.id,
                    **flags,
                ),
                owner=owner,
            )
        )
        await db.commit()
    return raw


async def actor_by_key(factory, raw: str) -> decl.Actor:
    request = Request({"type": "http", "method": "PUT", "path": "/", "headers": [(b"x-api-key", raw.encode())]})
    async with factory() as db:
        return await decl.actor_for(request, db, None)


async def test_an_api_key_declares_and_refreshes_without_stalling_the_writer(factory, tmp_path):
    owner = await a_user(factory)
    queue_id = await a_printer_queue(factory)
    path = tmp_path / "plate.gcode.3mf"
    a_3mf(path, "plate")
    archive_id = await an_archive(factory, path, owner=owner)
    job_id = await a_job(factory, archive_id, queue_id=queue_id)
    actor = await actor_by_key(factory, await a_key(factory, owner))
    assert actor.kind == "api_key"
    seen = await decl.inspect("archive", archive_id, actor=actor, rights=ALL)

    started = time.monotonic()
    declared = await decl.declare(
        "archive",
        archive_id,
        value=True,
        expected_sha256=seen["sha256"],
        expected_revision=seen["revision"],
        operation_id=str(uuid.uuid4()),
        actor=actor,
        rights=ALL,
    )
    listing = await jobs.candidates("archive", archive_id, actor=actor, rights=ALL, job_rights=QUEUE_ALL)
    refreshed = await jobs.refresh(
        "archive",
        archive_id,
        declaration_id=listing["declaration_id"],
        source_revision=listing["source_revision"],
        jobs=[ref(by_id(listing, "queue", job_id))],
        operation_id=str(uuid.uuid4()),
        actor=actor,
        rights=ALL,
        job_rights=QUEUE_ALL,
    )
    elapsed = time.monotonic() - started

    assert declared["value"] is True and refreshed["refreshed"] == [{"kind": "queue", "id": job_id}]
    assert elapsed < QUICK, f"held SQLite's writer for {elapsed:.1f}s"


async def test_a_key_disabled_while_the_bytes_were_read_commits_nothing(factory, tmp_path, monkeypatch):
    from fastapi import HTTPException
    from sqlalchemy import update

    from backend.app.services import queue_sources

    owner = await a_user(factory)
    path = tmp_path / "plate.gcode.3mf"
    a_3mf(path, "plate")
    archive_id = await an_archive(factory, path, owner=owner)
    actor = await actor_by_key(factory, await a_key(factory, owner))
    seen = await decl.inspect("archive", archive_id, actor=actor, rights=ALL)
    real_capture = queue_sources.capture

    async def capture_while_the_key_is_disabled(request):
        receipt = await real_capture(request)
        async with factory() as db:
            await db.execute(update(APIKey).values(enabled=False))
            await db.commit()
        return receipt

    monkeypatch.setattr(queue_sources, "capture", capture_while_the_key_is_disabled)
    with pytest.raises(HTTPException) as refused:
        await decl.declare(
            "archive",
            archive_id,
            value=True,
            expected_sha256=seen["sha256"],
            expected_revision=seen["revision"],
            operation_id=str(uuid.uuid4()),
            actor=actor,
            rights=ALL,
        )
    # C distinguishes an invalidated credential from a current key's missing grant.
    assert refused.value.status_code == 401
