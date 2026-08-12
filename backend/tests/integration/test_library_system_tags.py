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


# ---------------------------------------------------------------------------
# sync_system_tags — the single writer of both representations
# ---------------------------------------------------------------------------


async def _system_codes_of(db_session, file_id: int) -> list[str]:
    """The file's system tags, read THROUGH the association — never from the
    cache, or the test would be asking the cache whether the cache is right."""
    from backend.app.models.library import LibraryFileTag, LibraryTag

    rows = await db_session.execute(
        select(LibraryTag.code)
        .join(LibraryFileTag, LibraryFileTag.tag_id == LibraryTag.id)
        .where(LibraryFileTag.file_id == file_id, LibraryTag.is_system.is_(True))
    )
    return list(rows.scalars().all())


@pytest.fixture
async def system_tags(db_session):
    """The eleven catalog rows the migration seeds.

    Spelled out here rather than imported from the service: importing it would
    prove only that the service agrees with itself, and this list is the
    contract the migration also has to satisfy.
    """
    from backend.app.models.library import LibraryTag

    codes = ["3mf", "gcode", "stl", "obj", "step", "project", "geometry", "multiplate", "swap", "sliced", "makerworld"]
    for code in codes:
        db_session.add(LibraryTag(name=code.upper(), name_key=code, is_system=True, code=code))
    await db_session.commit()
    return codes


async def _file(db_session, **kwargs):
    from backend.app.models.library import LibraryFile

    defaults = {"filename": "cube.gcode.3mf", "file_path": "/tmp/c", "file_size": 1, "file_type": "gcode"}
    defaults.update(kwargs)
    row = LibraryFile(**defaults)
    db_session.add(row)
    await db_session.flush()
    return row


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sync_writes_both_representations(db_session, system_tags):
    """The cache and the rows are written by one function, in one place, at one
    moment — which is the whole reason keeping both is safe."""
    from backend.app.services.library_helpers import sync_system_tags

    f = await _file(db_session)

    codes = await sync_system_tags(db_session, f)
    await db_session.commit()

    assert set(f.file_tags) == set(codes)
    assert set(await _system_codes_of(db_session, f.id)) == set(codes)
    assert "gcode" in codes and "3mf" in codes


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sync_refuses_a_file_with_no_id(db_session, system_tags):
    """Associations key off file.id. Writing the cache and silently skipping the
    rows is the exact drift the single-writer design exists to prevent, so this
    fails loudly instead."""
    from backend.app.models.library import LibraryFile
    from backend.app.services.library_helpers import sync_system_tags

    unflushed = LibraryFile(filename="a.stl", file_path="/tmp/a", file_size=1, file_type="stl")

    with pytest.raises(ValueError):
        await sync_system_tags(db_session, unflushed)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sync_is_idempotent(db_session, system_tags):
    """The m128 backfill depends on this, and it is the only exercise the
    reconcile branch gets — no runtime path re-derives tags today."""
    from backend.app.services.library_helpers import sync_system_tags

    f = await _file(db_session)
    await sync_system_tags(db_session, f)
    await db_session.commit()
    first = sorted(await _system_codes_of(db_session, f.id))

    await sync_system_tags(db_session, f)
    await db_session.commit()

    assert sorted(await _system_codes_of(db_session, f.id)) == first
    assert first  # not vacuously equal because both are empty


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sync_drops_a_stale_system_tag(db_session, system_tags):
    """The reconcile half. Nothing re-derives tags at runtime today, but the
    backfill runs against rows whose codes may have moved on."""
    from backend.app.models.library import LibraryFileTag, LibraryTag
    from backend.app.services.library_helpers import sync_system_tags

    f = await _file(db_session, filename="a.stl", file_type="stl")
    stale = (await db_session.execute(select(LibraryTag.id).where(LibraryTag.code == "makerworld"))).scalar_one()
    db_session.add(LibraryFileTag(file_id=f.id, tag_id=stale))
    await db_session.commit()

    await sync_system_tags(db_session, f)
    await db_session.commit()

    assert "makerworld" not in await _system_codes_of(db_session, f.id)
    assert "stl" in await _system_codes_of(db_session, f.id)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sync_leaves_user_tags_alone(db_session, system_tags):
    """The reconcile deletes stale SYSTEM associations. If it filtered on the
    wrong thing it would quietly strip every label the user applied by hand, and
    nothing on screen would say so."""
    from backend.app.models.library import LibraryFileTag
    from backend.app.services.library_helpers import sync_system_tags

    user_tag = await _tag(db_session, name="kid-safe", name_key="kid-safe")
    f = await _file(db_session)
    db_session.add(LibraryFileTag(file_id=f.id, tag_id=user_tag.id))
    await db_session.commit()

    await sync_system_tags(db_session, f)
    await db_session.commit()

    rows = (
        (await db_session.execute(select(LibraryFileTag.tag_id).where(LibraryFileTag.file_id == f.id))).scalars().all()
    )
    assert user_tag.id in rows


@pytest.mark.asyncio
@pytest.mark.integration
async def test_every_library_file_construction_syncs_its_tags():
    """Six near-identical call sites, and a missed one produces a file that
    looks completely normal and is simply absent from every tag filter.

    A source guard rather than six end-to-end tests: three of the six sit deep
    inside routes (external scan, slicer output, ZIP extraction) that cannot be
    driven honestly in a unit test, and a path with NO test is exactly the one
    that gets left behind. This fails when a new construction site appears
    without a sync beside it.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    for rel in ("backend/app/api/routes/library.py", "backend/app/services/calibration_service.py"):
        source = (root / rel).read_text(encoding="utf-8")
        for match in re.finditer(r"LibraryFile\(", source):
            following = source[match.end() : match.end() + 3000]
            assert "sync_system_tags" in following, (
                f"{rel}: a LibraryFile is constructed at offset {match.start()} with no "
                "sync_system_tags within the next 3000 characters — that file would carry "
                "no system tags and vanish from every tag filter."
            )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_listing_returns_only_user_tags(async_client: AsyncClient, db_session, system_tags):
    """``LibraryFile.tags`` now spans BOTH kinds, because system tags are rows in
    the same association table. The listing must still hand back only the
    user-authored ones.

    Two reasons, and the first is immediate: the frontend renders ``file.tags``
    as green user pills, so leaking system tags here puts a second "3MF" pill on
    every card beside the badge that already says it. The second is that the
    row would carry 2-5 extra objects duplicating what ``file_tags`` said one
    field earlier — associations serve queries, the column serves rendering.
    """
    from backend.app.models.library import LibraryFileTag

    user_tag = await _tag(db_session, name="kid-safe", name_key="kid-safe")
    f = await _file(db_session, filename="cube.gcode.3mf")
    from backend.app.services.library_helpers import sync_system_tags

    await sync_system_tags(db_session, f)
    db_session.add(LibraryFileTag(file_id=f.id, tag_id=user_tag.id))
    await db_session.commit()

    rows = (await async_client.get("/api/v1/library/files")).json()
    row = next(r for r in rows if r["filename"] == "cube.gcode.3mf")

    assert [t["name"] for t in row["tags"]] == ["kid-safe"]
    # The system tags are still there — in the field built for them.
    assert "gcode" in row["file_tags"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_uploaded_file_gets_its_system_tags(async_client: AsyncClient, db_session, system_tags, tmp_path):
    """The one path that can be driven end to end — upload."""
    from backend.app.models.library import LibraryFile

    payload = tmp_path / "cube.stl"
    payload.write_bytes(b"solid cube\nendsolid cube\n")
    with payload.open("rb") as fh:
        response = await async_client.post(
            "/api/v1/library/files?generate_stl_thumbnails=false",
            files={"file": ("cube.stl", fh, "application/octet-stream")},
        )
    assert response.status_code in (200, 201), response.text

    file_id = (await db_session.execute(select(LibraryFile.id).where(LibraryFile.filename == "cube.stl"))).scalar_one()
    cached = (await db_session.execute(select(LibraryFile.file_tags).where(LibraryFile.id == file_id))).scalar_one()

    assert "stl" in cached
    assert set(await _system_codes_of(db_session, file_id)) == set(cached)
