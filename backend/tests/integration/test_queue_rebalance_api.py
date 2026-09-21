"""The two manual doors to rebalancing: an order line's button and the panel's per-item action.

Both run the same procedure with the setting OFF and the cooldown ignored; the
per-item one answers, for every id it was given, either a move or the reason.
"""

import hashlib
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.auth import create_access_token, get_password_hash
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.group import Group
from backend.app.models.queue_source import FORMAT_3MF, STATE_READY, QueueSource
from backend.app.models.user import User
from backend.app.services import queue_rebalance, queue_sources
from backend.app.services.farm_forecast import model_key
from backend.tests.integration.test_auto_queue_scheduler import (
    _make_printer_with_queue,
    _patch_printer_manager,
    rebalance_farm,
)
from backend.tests.integration.test_orders_api import _the_dialogs_saved_preference

pytestmark = pytest.mark.integration


async def _pending(db):
    return list((await db.execute(select(AutoQueueItem).where(AutoQueueItem.status == "pending"))).scalars().all())


async def _as_viewer(client, db):
    """Re-point the client at a Viewers-group user — read permissions only."""
    viewer = User(username="viewer", password_hash=get_password_hash("Viewer_Pass1!"), role="user", is_active=True)
    group = (await db.execute(select(Group).where(Group.name == "Viewers"))).scalar_one()
    viewer.groups.append(group)
    db.add(viewer)
    await db.commit()
    client.headers["Authorization"] = f"Bearer {create_access_token(data={'sub': 'viewer'})}"


@pytest.mark.asyncio
async def test_line_button_moves_with_the_setting_off_and_reports_the_counts(
    committing_client, db_session, printer_factory, tmp_path
):
    farm = await rebalance_farm(db_session, printer_factory, tmp_path)
    await _the_dialogs_saved_preference(db_session, printer_model="A1MINI")
    p_elig, p_sched = _patch_printer_manager({farm.p1s.id, farm.mini.id})
    with p_elig, p_sched:
        r = await committing_client.post(f"/api/v1/projects/{farm.project.id}/lines/{farm.line.id}/rebalance")
    assert r.status_code == 200, r.text
    assert r.json() == {"converted": 1, "created": 2, "cancelled": 0, "moved_parts": 6, "skipped": []}

    # ``committing_client`` writes through its own session, not ``db_session`` —
    # with expire_on_commit=False, farm.item stays cached at its pre-request
    # values unless expired (the established pattern in test_orders_api.py).
    db_session.expire_all()
    rows = await _pending(db_session)
    assert len(rows) == 3 and {model_key(row.target_model) for row in rows} == {model_key("A1MINI")}
    created = [row for row in rows if row.id != farm.item.id]
    # The created rows carry the operator's saved profile for the receiving model
    # (``_PROFILE_TOGGLES``: timelapse on, external storage), the converted one keeps its own options.
    assert all(row.timelapse is True and row.timelapse_storage == "external" for row in created)
    converted = next(row for row in rows if row.id == farm.item.id)
    assert converted.timelapse is False and converted.position == 1
    assert {row.batch_id for row in rows} == {converted.batch_id} and converted.batch_id


@pytest.mark.asyncio
async def test_the_companions_inherit_the_source_rows_job_flags(
    committing_client, db_session, printer_factory, tmp_path
):
    """The profile decides the TOGGLES, the source row decides the JOB.

    One external-only print that switches the printer off after it must not
    become one external-only print plus two AMS prints that leave it on: the
    saved profile carries none of those four fields, so without the row they
    would be the writer's defaults.
    """
    farm = await rebalance_farm(db_session, printer_factory, tmp_path)
    await _the_dialogs_saved_preference(db_session, printer_model="A1MINI")
    farm.item.use_ams = False
    farm.item.feed_policy = "external_only"
    farm.item.auto_off_after = True
    farm.item.require_previous_success = True
    await db_session.commit()

    p_elig, p_sched = _patch_printer_manager({farm.p1s.id, farm.mini.id})
    with p_elig, p_sched:
        r = await committing_client.post(f"/api/v1/projects/{farm.project.id}/lines/{farm.line.id}/rebalance")
    assert r.status_code == 200, r.text
    assert (r.json()["converted"], r.json()["created"]) == (1, 2)

    db_session.expire_all()
    rows = await _pending(db_session)
    assert len(rows) == 3
    for row in rows:
        assert (row.use_ams, row.feed_policy, row.auto_off_after, row.require_previous_success) == (
            False,
            "external_only",
            True,
            True,
        ), f"row {row.id} did not inherit the job flags"


@pytest.mark.asyncio
async def test_line_button_ignores_the_cooldown(committing_client, db_session, printer_factory, tmp_path):
    farm = await rebalance_farm(db_session, printer_factory, tmp_path)
    p_elig, p_sched = _patch_printer_manager({farm.p1s.id, farm.mini.id})
    with p_elig, p_sched:
        first = await committing_client.post(f"/api/v1/projects/{farm.project.id}/lines/{farm.line.id}/rebalance")
    assert first.json()["converted"] == 1
    mini2, _q = await _make_printer_with_queue(db_session, printer_factory, name="Mini-2", model="A1MINI")
    db_session.add(
        AutoQueueItem(
            library_file_id=farm.big.id,
            plate_id=1,
            project_id=farm.project.id,
            project_line_id=farm.line.id,
            target_model="P1S",
            required_filament_types='["PLA"]',
            print_time_seconds=3600,
            status="pending",
            position=9,
        )
    )
    await db_session.commit()
    p_elig, p_sched = _patch_printer_manager({farm.p1s.id, farm.mini.id, mini2.id})
    with p_elig, p_sched:
        second = await committing_client.post(f"/api/v1/projects/{farm.project.id}/lines/{farm.line.id}/rebalance")
    assert second.status_code == 200, second.text
    assert second.json()["converted"] == 1, "force: the five-minute pause does not apply to the button"


@pytest.mark.asyncio
async def test_line_button_refuses_a_line_of_another_order(committing_client, db_session, printer_factory, tmp_path):
    farm = await rebalance_farm(db_session, printer_factory, tmp_path)
    other = await committing_client.post("/api/v1/projects/", json={"name": "Other"})
    assert other.status_code in (200, 201), other.text
    r = await committing_client.post(f"/api/v1/projects/{other.json()['id']}/lines/{farm.line.id}/rebalance")
    assert r.status_code == 404
    assert r.json()["detail"] == "Order line not found in this project"
    assert (await committing_client.post(f"/api/v1/projects/9999/lines/{farm.line.id}/rebalance")).status_code == 404


@pytest.mark.asyncio
async def test_line_button_needs_projects_update_and_queue_update_all(
    committing_client, db_session, printer_factory, tmp_path
):
    farm = await rebalance_farm(db_session, printer_factory, tmp_path)
    await _as_viewer(committing_client, db_session)
    r = await committing_client.post(f"/api/v1/projects/{farm.project.id}/lines/{farm.line.id}/rebalance")
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_panel_action_moves_the_named_items_and_names_why_the_others_stay(
    async_client, db_session, printer_factory, tmp_path
):
    farm = await rebalance_farm(db_session, printer_factory, tmp_path)
    staged = AutoQueueItem(
        library_file_id=farm.big.id,
        plate_id=1,
        project_id=farm.project.id,
        project_line_id=farm.line.id,
        target_model="P1S",
        print_time_seconds=3600,
        status="pending",
        position=2,
        manual_start=True,
    )
    unfiled = AutoQueueItem(library_file_id=farm.big.id, plate_id=1, target_model="P1S", status="pending", position=3)
    pinned = AutoQueueItem(
        library_file_id=farm.big.id,
        plate_id=1,
        project_id=farm.project.id,
        project_line_id=farm.line.id,
        target_model="P1S",
        status="pending",
        position=4,
        filament_overrides=json.dumps([{"slot_id": 1, "type": "PLA", "color": "#FFFFFF", "force_color_match": True}]),
    )
    db_session.add_all([staged, unfiled, pinned])
    await db_session.commit()

    p_elig, p_sched = _patch_printer_manager({farm.p1s.id, farm.mini.id})
    with p_elig, p_sched:
        r = await async_client.post(
            "/api/v1/auto-queue/rebalance",
            json={"item_ids": [farm.item.id, staged.id, unfiled.id, pinned.id, 999_999]},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["converted"], body["created"], body["moved_parts"]) == (1, 2, 6)
    assert sorted(body["skipped"], key=lambda s: s["item_id"]) == sorted(
        [
            {"item_id": staged.id, "reason": "staged"},
            {"item_id": unfiled.id, "reason": "not_filed"},
            {"item_id": pinned.id, "reason": "pinned"},
            {"item_id": 999_999, "reason": "not_found"},
        ],
        key=lambda s: s["item_id"],
    )
    await db_session.refresh(farm.item)
    assert model_key(farm.item.target_model) == model_key("A1MINI") and farm.item.rebalanced_from_model == "P1S"


@pytest.mark.asyncio
async def test_panel_action_validates_the_id_list(async_client):
    assert (await async_client.post("/api/v1/auto-queue/rebalance", json={"item_ids": []})).status_code == 422
    assert (
        await async_client.post("/api/v1/auto-queue/rebalance", json={"item_ids": list(range(65))})
    ).status_code == 422


@pytest.mark.asyncio
async def test_panel_action_needs_queue_update_all(async_client, db_session, printer_factory, tmp_path):
    farm = await rebalance_farm(db_session, printer_factory, tmp_path)
    await _as_viewer(async_client, db_session)
    r = await async_client.post("/api/v1/auto-queue/rebalance", json={"item_ids": [farm.item.id]})
    assert r.status_code == 403, r.text


# --------------------------------------------------------------------------- #
# m173 — a move changes the FILE, so it must change the bytes with it
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def clean_spool_state():
    """Process-global capture state must not travel between tests."""
    queue_sources._reset_state()
    yield
    deadline = time.monotonic() + 10
    while queue_sources.active_captures() and time.monotonic() < deadline:  # pragma: no cover - drain
        time.sleep(0.01)
    queue_sources._reset_state()


async def _plant_blob(db, path: Path) -> QueueSource:
    """A ``queue_sources`` row for bytes that were captured before this test.

    Only the row: nothing in the rebalance reads a blob's file, and the point of
    planting one is that the row starts out naming the OLD model's copy — which is
    exactly the state Task 6's C1 leaves behind and what a restore must put back.
    """
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    row = QueueSource(
        sha256=digest,
        size_bytes=path.stat().st_size,
        relative_path=queue_sources.object_relative_path(digest, FORMAT_3MF),
        format=FORMAT_3MF,
        state=STATE_READY,
    )
    db.add(row)
    await db.flush()
    return row


async def _a_captured_farm(db_session, printer_factory, tmp_path):
    """``rebalance_farm`` whose pending row already owns a copy of the P1S file."""
    farm = await rebalance_farm(db_session, printer_factory, tmp_path)
    old = await _plant_blob(db_session, Path(farm.big.file_path))
    farm.item.queue_source_id = old.id
    farm.item.source_snapshot = {
        "version": 1,
        "provenance": {"kind": "library_file", "id": farm.big.id},
        "display_filename": farm.big.filename,
        "format": FORMAT_3MF,
        "plate_fallback": None,
    }
    await db_session.commit()
    farm.old_blob = old
    return farm


async def _move(db_session, farm):
    p_elig, p_sched = _patch_printer_manager({farm.p1s.id, farm.mini.id})
    with p_elig, p_sched:
        return await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)


@pytest.mark.asyncio
async def test_a_moved_row_names_the_TARGET_files_bytes(db_session, printer_factory, tmp_path):
    """Task 6 review C1: the converted row used to keep the OLD model's blob.

    Every reader resolves through the snapshot now, so a row that names the target
    model's file in its columns and the source model's bytes in ``queue_source_id``
    prints the wrong part on the wrong machine, and nothing in the row looks wrong.
    """
    farm = await _a_captured_farm(db_session, printer_factory, tmp_path)
    # Read before the expire below: ``expire_all`` turns every later attribute
    # access on these into a lazy load, which under the async session is a
    # MissingGreenlet rather than a query.
    item_id, old_blob_id = farm.item.id, farm.old_blob.id
    small_id, small_name = farm.small.id, farm.small.filename
    target_sha = hashlib.sha256(Path(farm.small.file_path).read_bytes()).hexdigest()

    result = await _move(db_session, farm)

    assert (result.converted, result.created) == (1, 2), result.skipped
    db_session.expire_all()
    rows = await _pending(db_session)
    assert len(rows) == 3
    for row in rows:
        assert row.library_file_id == small_id
        blob = await db_session.get(QueueSource, row.queue_source_id)
        assert blob is not None, f"row {row.id} has no bytes of its own after the move"
        assert blob.sha256 == target_sha, f"row {row.id} still names the OLD model's file"
    converted = next(row for row in rows if row.id == item_id)
    assert converted.queue_source_id != old_blob_id
    assert converted.source_snapshot["provenance"] == {"kind": "library_file", "id": small_id}
    assert converted.source_snapshot["display_filename"] == small_name


@pytest.mark.asyncio
async def test_the_capture_happens_before_the_row_is_mutated(db_session, printer_factory, tmp_path, monkeypatch):
    """A10: a correct NEW snapshot, or an UNCHANGED old job — never a half-move.

    Capturing after the conversion would satisfy the assertion above and still be
    wrong: the row would be durable on the target file with no bytes behind it for
    as long as the copy runs.
    """
    farm = await _a_captured_farm(db_session, printer_factory, tmp_path)
    untouched = (farm.big.id, farm.old_blob.id, "P1S")
    seen: list[tuple] = []
    real = queue_sources.capture

    async def spy(request):
        seen.append((farm.item.library_file_id, farm.item.queue_source_id, farm.item.target_model))
        return await real(request)

    monkeypatch.setattr(queue_sources, "capture", spy)
    result = await _move(db_session, farm)

    assert result.converted == 1, result.skipped
    assert seen, "the move never captured anything"
    assert seen[0] == untouched, "the row was already mutated when the copy started"


@pytest.mark.asyncio
async def test_a_refused_capture_leaves_the_job_exactly_as_it_was(db_session, printer_factory, tmp_path, monkeypatch):
    """A10's other half: the refusal is a ``refusal``, not a half-moved row."""
    farm = await _a_captured_farm(db_session, printer_factory, tmp_path)
    before = {
        name: getattr(farm.item, name)
        for name in (
            "library_file_id",
            "plate_id",
            "target_model",
            "required_filament_types",
            "print_time_seconds",
            "batch_id",
            "rebalanced_at",
            "rebalanced_from_model",
            "queue_source_id",
            "source_snapshot",
        )
    }
    monkeypatch.setattr(
        queue_sources, "capture", AsyncMock(side_effect=queue_sources.SourceUnreadable("the share went away"))
    )

    result = await _move(db_session, farm)

    assert (result.converted, result.created, result.moved_parts) == (0, 0, 0)
    assert result.skipped == [(farm.item.id, "source_unreadable")]
    db_session.expire_all()
    rows = await _pending(db_session)
    assert len(rows) == 1, "a refused move creates no companions"
    assert {name: getattr(rows[0], name) for name in before} == before


@pytest.mark.asyncio
async def test_a_failed_companion_creation_restores_the_old_bytes_too(
    db_session, printer_factory, tmp_path, monkeypatch
):
    """The undo is field by field, and the two snapshot columns are two of them.

    A converted row whose companions were never created under-covers its line;
    one that kept the new bytes while every other column went back would print the
    target model's plate on the source model's printer.
    """
    farm = await _a_captured_farm(db_session, printer_factory, tmp_path)
    item_id, big_id, old_blob_id = farm.item.id, farm.big.id, farm.old_blob.id
    monkeypatch.setattr(
        queue_rebalance, "add_items_to_auto_queue", AsyncMock(side_effect=RuntimeError("the writer is down"))
    )

    result = await _move(db_session, farm)

    assert (result.converted, result.created, result.moved_parts) == (0, 0, 0)
    assert result.skipped == [(item_id, "creation_failed")]
    db_session.expire_all()
    rows = await _pending(db_session)
    assert len(rows) == 1
    assert (rows[0].library_file_id, rows[0].target_model) == (big_id, "P1S")
    assert rows[0].queue_source_id == old_blob_id, "the row kept the target file's bytes after the undo"
    assert rows[0].source_snapshot["provenance"] == {"kind": "library_file", "id": big_id}
    assert rows[0].rebalanced_at is None


@pytest.mark.asyncio
async def test_the_pin_is_held_before_the_reference_is_even_computed(
    db_session, printer_factory, tmp_path, monkeypatch
):
    """§9, the pin's own window: published, nothing owns it yet, and the collector cannot touch it.

    ``snapshot_for`` is a production call **inside** the window — under the pin,
    before the conversion's commit — so a spy there enters it without instrumenting
    the session. Two claims:

    * the blob is already pinned at that point, which fails immediately if anyone
      moves the ``pin`` below the assignments or replaces it;
    * and the **real** collector, run from inside the window, leaves the row's
      ``unreferenced_at`` alone. That is the behavioural claim ("the grace never
      beats a live pin") asked of the thing itself rather than of a mock: without
      the pin the blob is unowned in the database at that instant and the pass
      would mark it.

    What this does NOT claim: coverage of the one instant no hook can reach, between
    ``publish``'s guard release and ``pin``'s guard acquire. A pass landing there
    can only ever set the mark, release needs the full hour, and the next pass
    clears the mark as soon as the row owns the blob.
    """
    farm = await _a_captured_farm(db_session, printer_factory, tmp_path)
    window: dict = {}
    real_snapshot_for = queue_sources.snapshot_for
    real_commit = db_session.commit

    def snapshot_spy(receipt, source):
        window["pinned"] = queue_sources.pinned_source_ids()
        window["blob_id"] = source.id
        window["armed"] = True
        return real_snapshot_for(receipt, source)

    async def commit_spy(*args, **kwargs):
        if window.pop("armed", False):
            # Still inside the window: the blob is published, the row that will own
            # it is not committed, and the collector is about to be asked.
            window["report"] = await queue_sources.collect(force=True)
        return await real_commit(*args, **kwargs)

    monkeypatch.setattr(queue_sources, "snapshot_for", snapshot_spy)
    monkeypatch.setattr(db_session, "commit", commit_spy)
    result = await _move(db_session, farm)

    assert result.converted == 1, result.skipped
    assert window["blob_id"] in window["pinned"], "the pin must be held before the reference is computed"
    report = window["report"]
    assert (report.marked, report.released) == (0, 0), "the collector marked a blob a live pin was holding"
    monkeypatch.undo()
    db_session.expire_all()
    converted = next(row for row in await _pending(db_session) if row.id == farm.item.id)
    blob = await db_session.get(QueueSource, converted.queue_source_id)
    assert blob.unreferenced_at is None and blob.state == STATE_READY
    assert queue_sources.pinned_source_ids() == frozenset(), "the pin outlived the move"


@pytest.mark.asyncio
async def test_an_undo_with_no_companions_created_is_still_committed(
    db_session, test_engine, printer_factory, tmp_path, monkeypatch
):
    """The conversion is durable before the companions are attempted.

    So an undo that only assigned the old values back in memory would leave the
    converted row on disk — which is why the undo's commit is unconditional now,
    where it used to happen only when rows had been created.
    """
    farm = await _a_captured_farm(db_session, printer_factory, tmp_path)
    item_id, big_id, old_blob_id = farm.item.id, farm.big.id, farm.old_blob.id
    monkeypatch.setattr(
        queue_rebalance, "add_items_to_auto_queue", AsyncMock(side_effect=RuntimeError("the writer is down"))
    )

    result = await _move(db_session, farm)
    assert result.skipped == [(item_id, "creation_failed")]

    # A session that has never seen this row: the assertions are about the DATABASE,
    # not about what the caller's session happens to hold.
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as fresh:
        row = await fresh.get(AutoQueueItem, item_id)
        assert (row.library_file_id, row.target_model) == (big_id, "P1S")
        assert (row.queue_source_id, row.rebalanced_at) == (old_blob_id, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raised,expected",
    [
        (lambda: queue_sources.QueueSourceBusy("every worker is copying"), "source_copy_busy"),
        (lambda: queue_sources.StorageReplaced("a restore swapped the spool"), "source_copy_busy"),
        (lambda: queue_sources.CaptureTimeout("the share stalled"), "source_copy_busy"),
        (lambda: queue_sources.NoSpace("the volume is full"), "source_spool_full"),
        (lambda: queue_sources.WriteFailed("the write failed"), "source_spool_full"),
        (lambda: queue_sources.SourceUnreadable("the share went away"), "source_unreadable"),
        (lambda: queue_sources.SourceInvalid("not a zip"), "source_unreadable"),
    ],
)
async def test_the_panel_says_what_to_do_about_a_refused_copy(
    db_session, printer_factory, tmp_path, monkeypatch, raised, expected
):
    """Three answers, not one — and the difference is what the operator should do.

    Folded onto ``source_unreadable``, a batch add that merely saturated the capture
    workers printed "the target file could not be read" over a perfectly good file.
    That teaches the operator to ignore the reason column, and then the column is
    worthless the day it is finally right. A full spool is the most actionable cause
    in the set and used to read as a file problem.
    """
    farm = await _a_captured_farm(db_session, printer_factory, tmp_path)
    item_id, old_blob_id = farm.item.id, farm.old_blob.id
    monkeypatch.setattr(queue_sources, "capture", AsyncMock(side_effect=raised()))

    result = await _move(db_session, farm)

    assert (result.converted, result.created) == (0, 0)
    assert result.skipped == [(item_id, expected)]
    db_session.expire_all()
    rows = await _pending(db_session)
    assert len(rows) == 1 and rows[0].queue_source_id == old_blob_id, "a refusal moves nothing, whatever it was"
