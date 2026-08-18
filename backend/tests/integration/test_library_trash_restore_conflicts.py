"""Restoring is the one way byte-identical duplicates can still come back.

Every ingest path refuses to create one and m141 cleared out what had already
accumulated — but ``restore`` simply clears ``deleted_at``. Pulling a file out of
the trash while its twin is active recreates exactly the pair the rest of the
feature exists to prevent, so it asks first.

⚠️ It asks, it does not refuse. A duplicate can be deliberate — two MakerWorld
profiles produce byte-identical 3MFs — and the user is the one who put this file
in the trash and is now taking it back out.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.library import LibraryFile


async def _file(db, *, filename: str, file_hash: str | None, trashed: bool) -> LibraryFile:
    from datetime import datetime, timezone

    row = LibraryFile(
        filename=filename,
        file_path=f"files/{filename}",
        file_type="3mf",
        file_size=10,
        file_hash=file_hash,
        deleted_at=datetime.now(timezone.utc) if trashed else None,
    )
    db.add(row)
    await db.flush()
    return row


@pytest.mark.asyncio
@pytest.mark.integration
async def test_restoring_onto_an_active_twin_asks_first(async_client: AsyncClient, db_session):
    active = await _file(db_session, filename="already-here.3mf", file_hash="same", trashed=False)
    trashed = await _file(db_session, filename="from-trash.3mf", file_hash="same", trashed=True)
    await db_session.commit()

    response = await async_client.post(f"/api/v1/library/trash/{trashed.id}/restore")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "duplicate_of_active"
    assert detail["existing_id"] == active.id
    assert detail["existing_filename"] == "already-here.3mf", "the question has to name the file"
    await db_session.refresh(trashed)
    assert trashed.deleted_at is not None, "a refused restore must not restore"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_force_restores_it_anyway(async_client: AsyncClient, db_session):
    await _file(db_session, filename="already-here.3mf", file_hash="same", trashed=False)
    trashed = await _file(db_session, filename="from-trash.3mf", file_hash="same", trashed=True)
    await db_session.commit()

    response = await async_client.post(f"/api/v1/library/trash/{trashed.id}/restore?force=true")

    assert response.status_code == 200
    await db_session.refresh(trashed)
    assert trashed.deleted_at is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_file_with_no_active_twin_restores_without_a_question(async_client: AsyncClient, db_session):
    trashed = await _file(db_session, filename="lonely.3mf", file_hash="unique", trashed=True)
    await db_session.commit()

    response = await async_client.post(f"/api/v1/library/trash/{trashed.id}/restore")

    assert response.status_code == 200
    await db_session.refresh(trashed)
    assert trashed.deleted_at is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_row_without_a_hash_is_never_blocked(async_client: AsyncClient, db_session):
    """Old external rows carried no hash. An unanswerable question is not a
    conflict, and must not become one that cannot be cleared."""
    await _file(db_session, filename="active.3mf", file_hash=None, trashed=False)
    trashed = await _file(db_session, filename="hashless.3mf", file_hash=None, trashed=True)
    await db_session.commit()

    response = await async_client.post(f"/api/v1/library/trash/{trashed.id}/restore")

    assert response.status_code == 200
    await db_session.refresh(trashed)
    assert trashed.deleted_at is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_check_returns_exactly_the_conflicting_ids(async_client: AsyncClient, db_session):
    """What lets a bulk restore ask once, with the list, instead of failing
    partway through."""
    active = await _file(db_session, filename="already-here.3mf", file_hash="same", trashed=False)
    conflicting = await _file(db_session, filename="dupe.3mf", file_hash="same", trashed=True)
    clean = await _file(db_session, filename="fine.3mf", file_hash="other", trashed=True)
    await db_session.commit()

    body = (
        await async_client.post(
            "/api/v1/library/trash/restore-check",
            json={"ids": [conflicting.id, clean.id]},
        )
    ).json()

    assert [c["id"] for c in body] == [conflicting.id]
    assert body[0]["existing_id"] == active.id
    assert body[0]["filename"] == "dupe.3mf"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_check_writes_nothing(async_client: AsyncClient, db_session):
    await _file(db_session, filename="already-here.3mf", file_hash="same", trashed=False)
    trashed = await _file(db_session, filename="dupe.3mf", file_hash="same", trashed=True)
    await db_session.commit()

    await async_client.post("/api/v1/library/trash/restore-check", json={"ids": [trashed.id]})

    await db_session.refresh(trashed)
    assert trashed.deleted_at is not None, "asking a question must not answer it"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_unknown_id_does_not_cost_the_rest_of_the_answer(async_client: AsyncClient, db_session):
    await _file(db_session, filename="already-here.3mf", file_hash="same", trashed=False)
    conflicting = await _file(db_session, filename="dupe.3mf", file_hash="same", trashed=True)
    await db_session.commit()

    body = (
        await async_client.post(
            "/api/v1/library/trash/restore-check",
            json={"ids": [999_999, conflicting.id]},
        )
    ).json()

    assert [c["id"] for c in body] == [conflicting.id]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_trashed_twin_is_not_a_conflict(async_client: AsyncClient, db_session):
    """Two files in the trash together: restoring one recreates nothing, because
    the other is not in the library either. ``find_reusable_row`` only ever looks
    at active rows, and this is the test that says so from here."""
    first = await _file(db_session, filename="a.3mf", file_hash="same", trashed=True)
    await _file(db_session, filename="b.3mf", file_hash="same", trashed=True)
    await db_session.commit()

    response = await async_client.post(f"/api/v1/library/trash/{first.id}/restore")

    assert response.status_code == 200
    # ⚠️ The endpoint committed in its OWN session; this one still holds the rows
    # it created in its identity map, so a plain select would answer from before
    # the restore. Expire first, or the assertion measures the test's cache.
    db_session.expire_all()
    survivors = (await db_session.execute(select(LibraryFile).where(LibraryFile.file_hash == "same"))).scalars().all()
    assert sum(1 for r in survivors if r.deleted_at is None) == 1
