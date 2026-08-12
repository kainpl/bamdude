"""A system tag is handed out by the system: not renamed, not deleted, not
detached. Every route that could do one of those has to say no.

The sharp one is ``bulk-assign``'s ``replace``: it deleted EVERY association on
the selected files. Once system tags are associations that silently strips
them — and because the ``file_tags`` cache is untouched, the badges keep
rendering while the file disappears from every filter. Two representations
disagreeing is precisely what the single-writer design exists to prevent.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select


async def _tag(db_session, **kwargs):
    from backend.app.models.library import LibraryTag

    defaults = {"name": "kid-safe", "name_key": "kid-safe"}
    defaults.update(kwargs)
    row = LibraryTag(**defaults)
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


async def _system_tag(db_session, code: str):
    return await _tag(db_session, name=code.upper(), name_key=code, is_system=True, code=code)


async def _file(db_session, **kwargs):
    from backend.app.models.library import LibraryFile

    defaults = {"filename": "a.stl", "file_path": "/tmp/a", "file_size": 1, "file_type": "stl"}
    defaults.update(kwargs)
    row = LibraryFile(**defaults)
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_system_tag_cannot_be_renamed(async_client: AsyncClient, db_session):
    from backend.app.models.library import LibraryTag

    tag = await _system_tag(db_session, "3mf")

    response = await async_client.patch(f"/api/v1/library/tags/{tag.id}", json={"name": "Renamed"})

    assert response.status_code == 400
    # The column, not the mapped object: the identity map hands back whatever
    # the request already set, so a mapped read can pass over a changed row.
    name = (await db_session.execute(select(LibraryTag.name).where(LibraryTag.id == tag.id))).scalar_one()
    assert name != "Renamed"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_system_tag_cannot_be_deleted(async_client: AsyncClient, db_session):
    from backend.app.models.library import LibraryTag

    tag = await _system_tag(db_session, "sliced")

    response = await async_client.delete(f"/api/v1/library/tags/{tag.id}")

    assert response.status_code == 400
    still_there = (await db_session.execute(select(LibraryTag.id).where(LibraryTag.id == tag.id))).scalar_one_or_none()
    assert still_there is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_user_tag_can_still_be_renamed_and_deleted(async_client: AsyncClient, db_session):
    """Guard on the guard: a refusal that hit everything would pass the two
    tests above while breaking the feature they protect."""
    tag = await _tag(db_session, name="toys", name_key="toys")

    renamed = await async_client.patch(f"/api/v1/library/tags/{tag.id}", json={"name": "toys v2"})
    assert renamed.status_code == 200

    deleted = await async_client.delete(f"/api/v1/library/tags/{tag.id}")
    assert deleted.status_code == 204


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_user_cannot_create_a_tag_named_after_a_system_one(async_client: AsyncClient, db_session):
    """The composite unique index deliberately ALLOWS this row — that is what
    lets an install with a pre-existing "sliced" tag migrate at all. So the
    refusal has to be an explicit check, and it has to say why rather than
    claiming a duplicate."""
    await _system_tag(db_session, "sliced")

    response = await async_client.post("/api/v1/library/tags", json={"name": "sliced"})

    assert response.status_code == 400
    assert "system" in response.json()["detail"].lower()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_bulk_add_ignores_a_system_tag(async_client: AsyncClient, db_session):
    from backend.app.models.library import LibraryFileTag

    system = await _system_tag(db_session, "stl")
    f = await _file(db_session)

    response = await async_client.post(
        "/api/v1/library/tags/bulk-assign",
        json={"file_ids": [f.id], "tag_ids": [system.id], "action": "add"},
    )
    assert response.status_code == 200

    rows = (
        (await db_session.execute(select(LibraryFileTag.tag_id).where(LibraryFileTag.file_id == f.id))).scalars().all()
    )
    assert system.id not in rows


@pytest.mark.asyncio
@pytest.mark.integration
async def test_bulk_remove_cannot_detach_a_system_tag(async_client: AsyncClient, db_session):
    from backend.app.models.library import LibraryFileTag

    system = await _system_tag(db_session, "stl")
    f = await _file(db_session)
    db_session.add(LibraryFileTag(file_id=f.id, tag_id=system.id))
    await db_session.commit()

    response = await async_client.post(
        "/api/v1/library/tags/bulk-assign",
        json={"file_ids": [f.id], "tag_ids": [system.id], "action": "remove"},
    )
    assert response.status_code == 200

    rows = (
        (await db_session.execute(select(LibraryFileTag.tag_id).where(LibraryFileTag.file_id == f.id))).scalars().all()
    )
    assert system.id in rows


@pytest.mark.asyncio
@pytest.mark.integration
async def test_replace_keeps_the_files_system_tags(async_client: AsyncClient, db_session):
    """Both halves are asserted: a "fix" that simply dropped ``replace`` would
    pass a test that only checked the system tags survived."""
    from backend.app.models.library import LibraryFileTag

    system = await _system_tag(db_session, "stl")
    old_user = await _tag(db_session, name="old", name_key="old")
    new_user = await _tag(db_session, name="new", name_key="new")
    f = await _file(db_session)
    db_session.add_all(
        [
            LibraryFileTag(file_id=f.id, tag_id=system.id),
            LibraryFileTag(file_id=f.id, tag_id=old_user.id),
        ]
    )
    await db_session.commit()

    response = await async_client.post(
        "/api/v1/library/tags/bulk-assign",
        json={"file_ids": [f.id], "tag_ids": [new_user.id], "action": "replace"},
    )
    assert response.status_code == 200

    tag_ids = set(
        (await db_session.execute(select(LibraryFileTag.tag_id).where(LibraryFileTag.file_id == f.id))).scalars().all()
    )
    assert system.id in tag_ids  # survived
    assert new_user.id in tag_ids  # applied
    assert old_user.id not in tag_ids  # replaced
