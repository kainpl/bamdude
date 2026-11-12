"""A library row's declaration carries its provenance, bound to its bytes (swap-source-declaration spec r3 §8; T9.3).

Every door that brings bytes in writes a hash-bound carrier with its origin -- ``classifier`` (upload, ZIP member,
scan, Virtual Printer, an automated import), ``generated`` (our own slices), ``transferred`` (an archive saved to the
library) -- together with the bool projection and the tags. A scan of new bytes on the same path writes the new hash
and a new classifier carrier together; the same bytes keep an operator's declaration; a NULL-hash row repaired by a
scan is classified by its name on the share. The DB-only backfill (m203's seed) persists §8's classification of every
legacy external default False, so the tags and the SQL tag filter agree with the effective fact.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.library import LibraryFile, LibraryFileTag, LibraryTag
from backend.app.services import swap_compatibility as sc
from backend.tests.integration.test_swap_library_ingest import (  # noqa: F401 -- fixtures
    _Request,
    a_3mf,
    a_captured_job,
    a_queue,
    clean_spool_state,
    fresh,
    mount,
    row_of,
    sessions,
    sha,
    touch_later,
    via_scan,
    via_upload,
    via_vp,
    via_zip,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def carrier_of(row: LibraryFile) -> dict | None:
    return row.swap_source_declaration


def classifier(value: bool, row: LibraryFile) -> dict:
    return {"version": 1, "value": value, "sha256": row.file_hash, "origin": "classifier"}


def an_operator_declaration(row: LibraryFile, value: bool) -> None:
    sc.write_source_declaration(
        row,
        value=value,
        sha256=row.file_hash,
        origin="operator",
        operator={
            "declaration_id": "1b4e28ba-2fa1-41d2-883f-0016d3cca427",
            "declared_at": "2026-10-14T10:00:00+00:00",
            "actor_id": None,
            "actor_label": "admin",
            "actor_kind": "user",
        },
    )


@pytest.mark.parametrize(("name", "declared"), [("plate.swaps.gcode.3mf", True), ("plate.gcode.3mf", False)])
async def test_every_ingest_door_writes_a_hash_bound_classifier_carrier(
    name, declared, committing_client, db_session, mount, tmp_path, test_engine
):
    root, folder = mount
    uploaded = a_3mf(tmp_path / "src" / "upload.3mf", "upload")
    zipped = a_3mf(tmp_path / "src" / "zip.3mf", "zip")
    scanned = a_3mf(root / name, "scan")
    received = a_3mf(tmp_path / "src" / "vp.3mf", "vp")

    await via_upload(committing_client, name, uploaded)
    await via_zip(committing_client, {f"models/{name}": zipped})
    await via_scan(db_session, folder)
    await via_vp(tmp_path, test_engine, name, received)

    for content in (uploaded, zipped, scanned, received):
        row = await row_of(db_session, content)
        assert carrier_of(row) == classifier(declared, row), carrier_of(row)
        assert row.swap_compatible is declared and sc.library_fact(row) is declared


async def test_an_automated_import_is_classified_and_a_saved_archive_is_transferred(
    committing_client, db_session, tmp_path, archive_factory, printer_factory
):
    from backend.app.api.routes.library import save_3mf_bytes_to_library
    from backend.tests.integration.test_swap_library_ingest import an_archive_file

    content = a_3mf(tmp_path / "src" / "mw.3mf", "makerworld")
    made = await save_3mf_bytes_to_library(
        db_session, content=content, filename="mw.swaps.3mf", source_type="makerworld"
    )
    row = await fresh(db_session, made.file.id)
    assert carrier_of(row) == classifier(True, row)

    printer = await printer_factory()
    archive, raw = await an_archive_file(
        db_session, archive_factory, printer.id, "plate.swap.3mf", "kept", record=False
    )
    response = await committing_client.post(f"/api/v1/archives/{archive.id}/save-to-library")
    assert response.status_code == 200, response.text
    saved = await row_of(db_session, raw)
    assert carrier_of(saved) == {"version": 1, "value": False, "sha256": saved.file_hash, "origin": "transferred"}


async def test_our_own_slices_are_generated(db_session, tmp_path, monkeypatch, sessions):
    from backend.app.api.routes import library as library_routes
    from backend.app.services import calibration_service
    from backend.app.services.calibration_service import _persist_calibration_slice_to_library
    from backend.app.services.slicer_api import SliceResult

    sliced = a_3mf(tmp_path / "out" / "sliced.gcode.3mf", "sliced output")

    async def slicer(db, **_kwargs):
        return SliceResult(content=sliced, print_time_seconds=60, filament_used_g=1.0, filament_used_mm=10.0), False

    async def no_preview(db, content, **_kwargs):
        return content

    monkeypatch.setattr(library_routes, "_run_slicer_with_fallback", slicer)
    monkeypatch.setattr(library_routes, "sliced_preview", no_preview)
    monkeypatch.setattr(calibration_service, "async_session", sessions)
    await library_routes.slice_and_persist(
        db_session,
        model_bytes=b"model",
        model_filename="plate.swap.3mf",
        folder_id=None,
        extra_metadata=None,
        request=_Request(),
        current_user_id=None,
    )
    calibration = a_3mf(tmp_path / "out" / "cal.gcode.3mf", "calibration output")
    file_id = await _persist_calibration_slice_to_library(
        content=calibration,
        filename="flow.swaps.gcode.3mf",
        user_id=None,
        print_time_seconds=None,
        filament_used_g=None,
        filament_used_mm=None,
    )

    for row in (await row_of(db_session, sliced), await fresh(db_session, file_id)):
        assert carrier_of(row) == {"version": 1, "value": False, "sha256": row.file_hash, "origin": "generated"}


async def test_new_bytes_on_the_share_write_the_new_hash_and_carrier_together(db_session, mount):
    root, folder = mount
    on_share = root / "plate.gcode.3mf"
    first = a_3mf(on_share, "v1")
    await via_scan(db_session, folder)
    row = await row_of(db_session, first)
    an_operator_declaration(row, True)  # declared for the first bytes
    await db_session.commit()

    second = a_3mf(on_share, "v2")
    touch_later(on_share)
    await via_scan(db_session, folder)

    row = await fresh(db_session, row.id)
    assert row.file_hash == sha(second)
    assert carrier_of(row) == classifier(False, row)  # the operator declared other bytes: nothing carried over


@pytest.mark.parametrize("refresh", ["hash_only", "re_extraction"])
async def test_the_same_bytes_keep_an_operators_declaration(refresh, db_session, mount):
    from backend.app.services.library_file_preparation import EXTRACTION_KEY

    root, folder = mount
    on_share = root / "plate.gcode.3mf"
    content = a_3mf(on_share, "v1")
    await via_scan(db_session, folder)
    row = await row_of(db_session, content)
    an_operator_declaration(row, True)
    row.filename = "renamed.swaps.3mf"  # a display rename re-judges nothing
    if refresh == "re_extraction":  # an older extraction: the same bytes are prepared and published again
        row.file_metadata = {**row.file_metadata, EXTRACTION_KEY: {**row.file_metadata[EXTRACTION_KEY], "version": 0}}
    await db_session.commit()
    declared = dict(carrier_of(row))

    touch_later(on_share)
    await via_scan(db_session, folder)

    row = await fresh(db_session, row.id)
    assert carrier_of(row) == declared and sc.library_fact(row) is True


async def test_a_failed_rescan_keeps_the_last_good_tuple(db_session, mount):
    root, folder = mount
    on_share = root / "plate.gcode.3mf"
    content = a_3mf(on_share, "v1")
    await via_scan(db_session, folder)
    row = await row_of(db_session, content)
    an_operator_declaration(row, True)
    await db_session.commit()
    before = (row.file_hash, dict(carrier_of(row)), row.swap_compatible)

    on_share.write_bytes(b"incomplete copy")
    touch_later(on_share)
    await via_scan(db_session, folder)

    row = await fresh(db_session, row.id)
    assert (row.file_hash, carrier_of(row), row.swap_compatible) == before


@pytest.mark.parametrize(("on_share", "declared"), [("plate.swaps.gcode.3mf", True), ("plate.gcode.3mf", False)])
async def test_a_hashless_legacy_row_is_classified_by_its_share_name_when_the_scan_repairs_it(
    on_share, declared, db_session, mount
):
    root, folder = mount
    content = a_3mf(root / on_share, "legacy")
    await via_scan(db_session, folder)
    row = await row_of(db_session, content)
    # What an older version left: no hash, the default False, no carrier -- and a display rename on top.
    row.file_hash, row.swap_compatible, row.swap_source_declaration = None, False, None
    row.filename = "renamed.3mf" if declared else "renamed.swaps.3mf"
    await db_session.commit()
    assert sc.library_fact(row) is None

    await via_scan(db_session, folder)

    row = await fresh(db_session, row.id)
    assert row.file_hash == sha(content)
    assert carrier_of(row) == classifier(declared, row)


async def test_a_duplicate_upload_does_not_repair_the_fact(committing_client, db_session, tmp_path):
    content = a_3mf(tmp_path / "src" / "dup.3mf", "dup")
    await via_upload(committing_client, "plate.gcode.3mf", content)
    row = await row_of(db_session, content)
    before = (dict(carrier_of(row)), row.swap_compatible)

    await via_upload(committing_client, "plate.swaps.gcode.3mf", content)  # the same bytes, another name

    row = await fresh(db_session, row.id)
    assert (carrier_of(row), row.swap_compatible) == before


async def test_a_captured_job_keeps_its_fact_and_a_new_capture_reads_the_declaration(
    committing_client, db_session, tmp_path, printer_factory, sessions
):
    content = a_3mf(tmp_path / "src" / "job.3mf", "job")
    await via_upload(committing_client, "plate.gcode.3mf", content)
    row = await row_of(db_session, content)
    queue = await a_queue(db_session, printer_factory)
    before = await a_captured_job(sessions, db_session, queue, row)
    an_operator_declaration(row, True)
    await db_session.commit()

    after = await a_captured_job(sessions, db_session, queue, await fresh(db_session, row.id))

    assert sc.snapshot_fact((await fresh_job(db_session, before.id)).source_snapshot).value is False
    assert sc.snapshot_fact(after.source_snapshot).value is True


async def fresh_job(db: AsyncSession, job_id: int):
    from backend.app.models.print_queue import PrintQueueItem

    return await db.get(PrintQueueItem, job_id, populate_existing=True)


# --- the tags and the backfill ---------------------------------------------------------------------------------------


async def a_legacy_external_row(db: AsyncSession, path: str, *, file_hash="ab" * 32, value=False, extra_tags=()) -> int:
    row = LibraryFile(
        filename="display.3mf",
        file_path=path,
        file_type="3mf",
        file_size=1,
        file_hash=file_hash,
        is_external=True,
        swap_compatible=value,
        file_tags=["3mf", *extra_tags],
    )
    db.add(row)
    await db.commit()
    return row.id


async def swap_tag_id(db: AsyncSession) -> int:
    return (await db.execute(select(LibraryTag.id).where(LibraryTag.code == "swap"))).scalar_one()


async def associations(db: AsyncSession, file_id: int) -> set[int]:
    return set((await db.execute(select(LibraryFileTag.tag_id).where(LibraryFileTag.file_id == file_id))).scalars())


async def run_backfill(test_engine) -> None:
    from backend.app.migrations import m203_swap_source_declaration as m203

    await m203.seed(async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False))


async def seed_system_tags(test_engine) -> None:
    """The catalog rows ``create_all`` does not make: m128's seed, as an upgraded install has them."""
    from backend.app.migrations import m128_library_system_tags as m128

    await m128.seed(async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False))


async def test_the_swap_tag_follows_the_effective_fact_before_and_after_the_backfill(
    committing_client, db_session, test_engine
):
    await seed_system_tags(test_engine)
    marked = await a_legacy_external_row(db_session, "/mnt/share/tray.swaps.gcode.3mf", extra_tags=("custom",))
    plain = await a_legacy_external_row(db_session, "/mnt/share/tray.gcode.3mf", file_hash="cd" * 32)
    swap = await swap_tag_id(db_session)

    listed = {f["id"]: f for f in (await committing_client.get("/api/v1/library/files")).json()}
    assert "swap" in listed[marked]["file_tags"] and "custom" in listed[marked]["file_tags"]
    assert "swap" not in listed[plain]["file_tags"]
    assert "swap" not in (await fresh(db_session, marked)).file_tags  # the answer wrote nothing
    assert swap not in await associations(db_session, marked)
    # The boundary: the persisted SQL filter and the catalog count converge after the backfill, not from a GET.
    assert (await committing_client.get("/api/v1/library/files", params={"tag_ids": [swap]})).json() == []

    await run_backfill(test_engine)

    row = await fresh(db_session, marked)
    assert row.swap_compatible is True and carrier_of(row) == classifier(True, row)
    assert set(row.file_tags) >= {"swap", "custom", "3mf"} and row.file_tags.count("swap") == 1
    assert swap in await associations(db_session, marked)
    filtered = (await committing_client.get("/api/v1/library/files", params={"tag_ids": [swap]})).json()
    assert [f["id"] for f in filtered] == [marked]
    paged = (
        await committing_client.get("/api/v1/library/files", params={"tag_ids": [swap], "page": 1, "per_page": 1})
    ).json()
    assert paged["meta"]["total"] == 1 and [f["id"] for f in paged["items"]] == [marked]
    catalog = {t["id"]: t for t in (await committing_client.get("/api/v1/library/tags")).json()}
    assert catalog[swap]["file_count"] == 1
    assert carrier_of(await fresh(db_session, plain)) == classifier(False, await fresh(db_session, plain))


async def test_the_backfill_touches_only_legacy_external_defaults_and_reruns_safely(db_session, test_engine):
    true_row = await a_legacy_external_row(db_session, "/mnt/share/a.gcode.3mf", value=True)
    managed = LibraryFile(
        filename="m.swaps.3mf",
        file_path="library/files/m.swaps.3mf",
        file_type="3mf",
        file_size=1,
        file_hash="ef" * 32,
        is_external=False,
        swap_compatible=False,
    )
    broken = LibraryFile(
        filename="b.3mf",
        file_path="/mnt/share/b.swaps.3mf",
        file_type="3mf",
        file_size=1,
        file_hash="12" * 32,
        is_external=True,
        swap_compatible=False,
        swap_source_declaration={"version": 1, "invalid": True},
    )
    hashless = LibraryFile(
        filename="h.3mf",
        file_path="/mnt/share/h.swaps.3mf",
        file_type="3mf",
        file_size=1,
        file_hash=None,
        is_external=True,
        swap_compatible=False,
    )
    malformed = LibraryFile(
        filename="x.3mf",
        file_path="/mnt/share/x.swaps.3mf",
        file_type="3mf",
        file_size=1,
        file_hash="NOT-A-SHA256",
        is_external=True,
        swap_compatible=False,
    )
    db_session.add_all([managed, broken, hashless, malformed])
    await db_session.commit()
    snapshot = {}
    for row_id in (true_row, managed.id, broken.id, hashless.id, malformed.id):
        row = await fresh(db_session, row_id)
        snapshot[row_id] = (row.swap_compatible, row.swap_source_declaration, list(row.file_tags or []))

    await run_backfill(test_engine)
    await run_backfill(test_engine)

    for row_id, before in snapshot.items():
        row = await fresh(db_session, row_id)
        assert (row.swap_compatible, row.swap_source_declaration, list(row.file_tags or [])) == before, row_id


async def test_a_makerworld_redownload_declares_its_new_bytes_by_the_rows_name(
    async_client, db_session, monkeypatch, tmp_path
):
    from backend.app.services.model_providers import makerworld_provider
    from backend.tests.integration.test_makerworld_provider_routes import _FakeMakerWorld

    on_disk = tmp_path / "library" / "files" / "tray.swaps.3mf"
    on_disk.parent.mkdir(parents=True)
    on_disk.write_bytes(b"old")
    row = LibraryFile(
        filename="tray.swaps.3mf",
        file_path="library/files/tray.swaps.3mf",
        file_type="3mf",
        file_size=3,
        file_hash=sha(b"old"),
        source_type="makerworld",
        source_url="https://makerworld.com/models/1400373",
    )
    an_operator_declaration(row, False)  # declared for the old bytes
    db_session.add(row)
    await db_session.commit()

    async def build_service(**_kwargs):
        return _FakeMakerWorld()

    monkeypatch.setattr(makerworld_provider, "build_service", build_service)
    monkeypatch.setattr("backend.app.core.config.settings.base_dir", tmp_path)

    response = await async_client.post(f"/api/v1/makerworld/imports/{row.id}/redownload")

    assert response.status_code == 200, response.text
    row = await fresh(db_session, row.id)
    from backend.app.api.routes.library import to_absolute_path

    published = to_absolute_path(row.file_path)
    assert published != on_disk and not on_disk.exists()
    assert row.file_hash == sha(published.read_bytes()) != sha(b"old")
    assert carrier_of(row) == classifier(True, row)  # nothing of the old bytes' declaration carried over
    assert "swap" in row.file_tags


async def test_a_file_renamed_on_the_share_is_a_new_ingestion_without_the_old_declaration(db_session, mount):
    root, folder = mount
    content = a_3mf(root / "plate.gcode.3mf", "v1")
    await via_scan(db_session, folder)
    row = await row_of(db_session, content)
    an_operator_declaration(row, True)
    await db_session.commit()
    old_id = row.id

    (root / "plate.gcode.3mf").rename(root / "moved.swaps.gcode.3mf")
    await via_scan(db_session, folder)

    moved_path = str(root / "moved.swaps.gcode.3mf")
    rows = (
        (
            await db_session.execute(
                select(LibraryFile).where(LibraryFile.file_path == moved_path).execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    assert [r.id for r in rows] != [old_id] and len(rows) == 1  # a new row at the new path, not the old one moved
    assert carrier_of(rows[0]) == classifier(True, rows[0])  # the new share name declares; nothing carried over
    assert carrier_of(rows[0]).get("origin") != "operator"
