"""System tags are ordinary rows in the catalog, marked as system.

The computed badges (m036's ``file_tags``) and the user catalog (m095's
``library_tags``) answered the same question — "show me files marked X" —
through two storage layers and two filters. System tags are now rows too.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError


async def _tag(db_session, **kwargs):
    from backend.app.models.library import LibraryTag

    defaults = {"name": "kid-safe", "name_key": "kid-safe"}
    defaults.update(kwargs)
    row = LibraryTag(**defaults)
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_user_tag_is_not_a_system_tag(db_session):
    """The default has to be False — every existing row is user-authored."""
    tag = await _tag(db_session)

    assert tag.is_system is False
    assert tag.code is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_system_tag_carries_its_code(db_session):
    tag = await _tag(db_session, name="3MF", name_key="3mf", is_system=True, code="3mf")

    assert tag.is_system is True
    assert tag.code == "3mf"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_user_tag_may_share_a_name_with_a_system_tag(db_session):
    """The composite unique. Without it, an install where somebody already made
    a tag called "sliced" could not be migrated at all without either renaming
    their data or prefixing every system row forever."""
    await _tag(db_session, name="SLICED", name_key="sliced", is_system=True, code="sliced")

    user_tag = await _tag(db_session, name="sliced", name_key="sliced")

    assert user_tag.id is not None
    assert user_tag.is_system is False


@pytest.mark.asyncio
@pytest.mark.integration
async def test_two_system_tags_cannot_share_a_code(db_session):
    """The code is what ``sync_system_tags`` looks rows up by, so a duplicate
    would make "the" row for a code ambiguous."""
    await _tag(db_session, name="3MF", name_key="3mf", is_system=True, code="3mf")

    with pytest.raises(IntegrityError):
        await _tag(db_session, name="3MF copy", name_key="3mf-copy", is_system=True, code="3mf")
    await db_session.rollback()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_catalog_reports_which_tags_are_system(async_client: AsyncClient, db_session):
    """The assertion that fails when the schema gains the fields and the three
    TagResponse construction sites do not: every row would come back False."""
    await _tag(db_session, name="3MF", name_key="3mf", is_system=True, code="3mf")

    response = await async_client.get("/api/v1/library/tags")
    assert response.status_code == 200
    row = next(r for r in response.json() if r["name"] == "3MF")

    assert row["is_system"] is True
    assert row["code"] == "3mf"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_catalog_still_reports_a_plain_user_tag(async_client: AsyncClient, db_session):
    """Guard on the guard above: if the response defaulted everything to True
    instead, the system assertion would pass for the wrong reason."""
    await _tag(db_session, name="kid-safe", name_key="kid-safe")

    response = await async_client.get("/api/v1/library/tags")
    row = next(r for r in response.json() if r["name"] == "kid-safe")

    assert row["is_system"] is False
    assert row["code"] is None


# Kept so the unused import is not a lint error before the sync tests land.
_ = select
