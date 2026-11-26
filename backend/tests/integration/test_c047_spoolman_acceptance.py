"""C047 part 2 through the real routes — the acceptance's regressions (A02, S30, S35, S36, explicit nulls).

Spec ``60-specs/c047-spool-id-never-reissued-spec.md`` (vault); the independent acceptance of the
implementation named the scenarios below. Spoolman is a local fake throughout; the delete door
is the real one, with its own fake remote client.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.app.models.settings import Settings
from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
from backend.tests.unit.test_g01_e_git_restore_actor import actor_restore  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

A, B = "http://a.invalid:7912", "http://b.invalid:7912"
TAG = "12345678ABCDEF00"
OTHER_TAG = "ABCDEF0012345678"


async def _seed_source(db) -> None:
    db.add_all([Settings(key="spoolman_enabled", value="true"), Settings(key="spoolman_url", value=A)])
    await db.commit()


class _Remote:
    """The delete door's own Spoolman: one spool, a DELETE that can be held."""

    def __init__(self, spool_id: int, tag: str):
        self.spool = {"id": spool_id, "extra": {"tag": json.dumps(tag)}, "tags": []}
        self.deleted: list[int] = []
        self.hold: asyncio.Event | None = None
        self.deleting = asyncio.Event()

    async def health_check(self):
        return True

    async def get_spool(self, spool_id):
        return dict(self.spool)

    async def delete_spool(self, spool_id):
        self.deleting.set()
        if self.hold is not None:
            await self.hold.wait()
        self.deleted.append(spool_id)

    async def close(self):
        pass


def _tray(tray_id: int, tag: str):
    return SimpleNamespace(
        ams_id=0,
        tray_id=tray_id,
        tray_type="PLA",
        tray_info_idx="",
        tray_uuid="",
        tag_uid=tag,
        remain=50,
        tray_weight=1000,
        tray_sub_brands="PLA",
        tray_color="FFFF00FF",
    )


def _creating_client(monkeypatch, trays: dict[int, SimpleNamespace]):
    """A real ``SpoolmanClient`` whose create path runs as written; its remote calls are local fakes."""
    from backend.app.services.spoolman import SpoolmanClient

    client = SpoolmanClient(A)
    monkeypatch.setattr(client, "health_check", AsyncMock(return_value=True))
    monkeypatch.setattr(client, "get_spools", AsyncMock(return_value=[]))
    monkeypatch.setattr(client, "parse_ams_tray", lambda ams_id, data: trays.get(int(data.get("id", 0))))
    monkeypatch.setattr(client, "find_spool_by_tag", AsyncMock(return_value=None))
    monkeypatch.setattr(client, "is_bambu_lab_spool", lambda *args: False)
    monkeypatch.setattr(client, "_holder_outside_the_list", AsyncMock(return_value=None))
    monkeypatch.setattr(client, "find_or_create_filament", AsyncMock(return_value=18))
    native = AsyncMock()
    monkeypatch.setattr(client, "add_native_tags", native)
    posted, deliver = asyncio.Event(), asyncio.Event()

    async def confirmed_post(**kwargs):
        posted.set()
        await deliver.wait()
        return {"id": 17}

    monkeypatch.setattr(client, "create_spool", confirmed_post)
    return client, native, posted, deliver


def _printer_state(tray_ids):
    return SimpleNamespace(state="IDLE", raw_data={"ams": [{"id": 0, "tray": [{"id": t} for t in tray_ids]}]})


# --- A02 / S37: a create the delete beat — whether still running or already over ---------


@pytest.mark.parametrize("delete_over", [True, False], ids=["delete-finished", "delete-running"])
async def test_from_slot_does_not_bind_a_number_a_delete_took(
    async_client, db_session, printer_factory, monkeypatch, delete_over
):
    from backend.app.api.routes import spoolman as routes
    from backend.app.services import spoolman_binding_fence, spoolman_delete

    await _seed_source(db_session)
    printer = await printer_factory()
    client, native, posted, deliver = _creating_client(monkeypatch, {0: _tray(0, TAG)})
    monkeypatch.setattr(routes, "get_spoolman_client", AsyncMock(return_value=client))
    monkeypatch.setattr(routes.printer_manager, "get_status", lambda *_: _printer_state([0]))
    remote = _Remote(17, TAG)
    if not delete_over:
        remote.hold = asyncio.Event()
    monkeypatch.setattr(spoolman_delete, "_client_for", lambda _url: remote)

    request = asyncio.create_task(
        async_client.post(
            "/api/v1/spoolman/spools/from-slot", json={"printer_id": printer.id, "ams_id": 0, "tray_id": 0}
        )
    )
    try:
        await asyncio.wait_for(posted.wait(), 5)
        deletion = asyncio.create_task(spoolman_delete.delete_spool(17))
        if delete_over:
            assert (await asyncio.wait_for(deletion, 5)).as_response() == {"status": "deleted"}
            assert not spoolman_binding_fence.deleting(17)
        else:
            await asyncio.wait_for(remote.deleting.wait(), 5)
            assert spoolman_binding_fence.deleting(17)
    finally:
        deliver.set()
        response = await asyncio.wait_for(request, 5)
        if remote.hold is not None:
            remote.hold.set()
            await asyncio.wait_for(deletion, 5)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "spool_created_not_bound"
    assert response.json()["detail"]["spool_id"] == 17
    native.assert_not_awaited()
    assert remote.deleted == [17]
    assert (await db_session.execute(select(SpoolmanSlotAssignment))).scalars().all() == []


async def test_a_manual_sync_reports_the_beaten_create_and_commits_the_other_trays(
    async_client, db_session, printer_factory, monkeypatch
):
    """A02: the refused tray is one error naming its number; the other tray reaches the batch commit."""
    from backend.app.api.routes import spoolman as routes
    from backend.app.services import spoolman_delete
    from backend.app.services.spoolman import ArchivedTagIndex

    await _seed_source(db_session)
    printer = await printer_factory()
    client, native, posted, deliver = _creating_client(monkeypatch, {0: _tray(0, TAG), 1: _tray(1, OTHER_TAG)})
    real_sync = client.sync_ams_tray

    async def per_tray(tray, *args, **kwargs):
        if tray.tray_id == 1:
            return {"id": 18}  # an existing spool found by its tag
        return await real_sync(tray, *args, **kwargs)

    monkeypatch.setattr(client, "sync_ams_tray", per_tray)
    monkeypatch.setattr(ArchivedTagIndex, "holds_archived_reel", AsyncMock(return_value=False))
    monkeypatch.setattr(routes, "get_spoolman_client", AsyncMock(return_value=client))
    monkeypatch.setattr(routes.printer_manager, "get_status", lambda *_: _printer_state([0, 1]))
    remote = _Remote(17, TAG)
    monkeypatch.setattr(spoolman_delete, "_client_for", lambda _url: remote)

    request = asyncio.create_task(async_client.post(f"/api/v1/spoolman/sync/{printer.id}"))
    try:
        await asyncio.wait_for(posted.wait(), 5)
        assert (await asyncio.wait_for(spoolman_delete.delete_spool(17), 5)).as_response() == {"status": "deleted"}
    finally:
        deliver.set()
        response = await asyncio.wait_for(request, 5)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is False
    assert [e for e in body["errors"] if "#17" in e] == [
        "AMS 0 tray 0: spool #17 was created in Spoolman but not bound because of a concurrent delete"
    ]
    native.assert_not_awaited()
    rows = (await db_session.execute(select(SpoolmanSlotAssignment))).scalars().all()
    assert [(r.tray_id, r.spoolman_spool_id) for r in rows] == [(1, 18)]


# --- S30: an authenticated Git SPOOLS restore holds the source to its commit -----------------


async def test_authenticated_git_spools_holds_the_source_through_its_commit(actor_restore, monkeypatch):  # noqa: F811
    from backend.app.api.routes.spoolman_usage import keep_previous_spoolman_usage_source
    from backend.app.models.spoolman_usage_history import SpoolmanUsageHistory
    from backend.app.schemas.git_backup import RestoreCategory
    from backend.app.services import spoolman_source
    from backend.app.services.git_restore import SPOOLMAN_USAGE_PATH, GitRestoreService

    factory, _, capture, config_id = actor_restore
    async with factory() as db:
        await _seed_source(db)
    service = GitRestoreService()
    service._resolve_ref = AsyncMock(return_value=("a" * 40, "", None))
    service._read_categories = AsyncMock(
        return_value=(
            {
                SPOOLMAN_USAGE_PATH: {
                    "usage_history": [
                        {
                            "spoolman_spool_id": 17,
                            "weight_used": 2.0,
                            "status": "completed",
                            "created_at": "2026-11-25T01:02:03",
                            "current_source": True,
                            "print_name": "fixture",
                        }
                    ]
                },
            },
            "",
        )
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original = service._restore_spoolman_usage

    async def stage(*args, **kwargs):
        await original(*args, **kwargs)
        assert spoolman_source.held_by_me()
        entered.set()
        await release.wait()

    monkeypatch.setattr(service, "_restore_spoolman_usage", stage)
    run = asyncio.create_task(
        service.run_restore(
            config_id,
            "HEAD",
            [RestoreCategory.SPOOLS],
            authority=capture,
            required_permissions=("inventory:update",),
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    racing = asyncio.Event()

    async def keep():
        async with factory() as db:
            racing.set()
            return await keep_previous_spoolman_usage_source(db=db, _=None)

    concurrent = asyncio.create_task(keep())
    await asyncio.wait_for(racing.wait(), 4)
    await asyncio.sleep(0.05)
    assert not concurrent.done(), "the racing change waits for the restore's commit"
    release.set()
    result, _ = await asyncio.wait_for(asyncio.gather(run, concurrent), 8)
    assert result["success"], result
    async with factory() as db:
        row = await db.scalar(select(SpoolmanUsageHistory).where(SpoolmanUsageHistory.spoolman_spool_id == 17))
        stored = await db.scalar(select(Settings.value).where(Settings.key == "spoolman_usage_source"))
        assert row.source_token == json.loads(stored)["token"]


# --- S36: the partial charge and the runout correction are one ownership -----------------------


async def test_the_partial_charge_and_the_runout_correction_own_one_source(db_session, printer_factory, monkeypatch):
    from backend.app.api.routes.settings import update_spoolman_settings
    from backend.app.core import database
    from backend.app.models.active_print_spoolman import ActivePrintSpoolman
    from backend.app.models.archive import PrintArchive
    from backend.app.models.print_usage_event import EVENT_RUNOUT, KIND_AUTOSWITCH, PrintUsageEvent
    from backend.app.services import spoolman, spoolman_tracking

    await _seed_source(db_session)
    printer = await printer_factory()
    row = PrintArchive(
        printer_id=printer.id, status="failed", filename="f.3mf", print_name="f", file_path="", file_size=0
    )
    db_session.add(row)
    await db_session.flush()
    db_session.add_all(
        [
            ActivePrintSpoolman(printer_id=printer.id, archive_id=row.id, filament_usage=[], ams_trays={}),
            PrintUsageEvent(
                printer_id=printer.id,
                archive_id=row.id,
                event=EVENT_RUNOUT,
                kind=KIND_AUTOSWITCH,
                global_tray_id=0,
                layer_num=10,
                spoolman_spool_id=17,
            ),
        ]
    )
    await db_session.commit()
    clients = []

    class LocalClient:
        def __init__(self, url):
            self.url = url
            self.charges = []
            clients.append(self)

        async def health_check(self):
            return True

        async def get_spool(self, number):
            return {"id": number, "remaining_weight": 3.0}

        async def use_spool(self, number, grams):
            self.charges.append((number, grams))

        async def close(self):
            pass

    before_correction, resume = asyncio.Event(), asyncio.Event()

    async def pause_partial(*args, charge, **kwargs):
        assert await charge.client() is clients[0]
        before_correction.set()
        await resume.wait()

    monkeypatch.setattr(spoolman_tracking, "async_session", database.async_session)
    monkeypatch.setattr(spoolman, "SpoolmanClient", LocalClient)
    monkeypatch.setattr(spoolman_tracking, "_report_partial_usage", pause_partial)

    async def run():
        async with database.async_session() as db:
            await spoolman_tracking.cleanup_tracking(printer.id, row.id, db)

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(before_correction.wait(), 5)
        async with database.async_session() as db:
            with pytest.raises(HTTPException) as refused:
                await update_spoolman_settings({"spoolman_url": B}, db=db, _=None)
        assert (refused.value.status_code, refused.value.detail["reason"]) == (409, "charge_window")
    finally:
        resume.set()
        await asyncio.wait_for(task, 5)
    assert len(clients) == 1 and clients[0].url == A and clients[0].charges == [(17, 3.0)]
    assert (
        await db_session.scalar(select(ActivePrintSpoolman.id).where(ActivePrintSpoolman.archive_id == row.id))
    ) is None


# --- an explicit null is refused, and changes nothing ------------------------------------------


@pytest.mark.parametrize("key", ["spoolman_enabled", "spoolman_url"])
async def test_an_explicit_null_is_refused_without_writing(async_client, db_session, key):
    from backend.app.services import spoolman_activity, spoolman_source

    await _seed_source(db_session)
    initial = spoolman_source.generation()
    spoolman_activity.enter_window()
    try:
        response = await async_client.patch("/api/v1/settings/", json={key: None})
    finally:
        spoolman_activity.leave_window()
    assert response.status_code == 422, response.text
    expected = {"spoolman_enabled": "true", "spoolman_url": A}[key]
    assert await db_session.scalar(select(Settings.value).where(Settings.key == key)) == expected
    assert spoolman_source.generation() == initial


# --- S35: the real ZIP restore refuses before it prepares anything while a charge runs ----------


async def test_the_zip_restore_refuses_while_a_spoolman_charge_runs(async_client, db_session, monkeypatch, tmp_path):
    import sqlite3
    from contextlib import closing
    from io import BytesIO
    from zipfile import ZipFile

    from backend.app.core import db_portable
    from backend.app.services import backup_files, obico_history_files, spoolman_charge, spoolman_source
    from backend.app.services.farm_startup_guard import farm_startup_guard
    from backend.app.services.spoolman_delete import delete_runtime

    await _seed_source(db_session)
    owner_ready, release = asyncio.Event(), asyncio.Event()

    async def charging():
        async with spoolman_charge.owning("C047 ZIP regression") as charge:
            assert charge is not None
            owner_ready.set()
            await release.wait()

    task = asyncio.create_task(charging())
    real_drain = delete_runtime.quiesce_for_restore

    async def bounded():
        assert not spoolman_source.charge_gate_open() and not delete_runtime.admitting
        return await real_drain(timeout=0.05)

    monkeypatch.setattr(delete_runtime, "quiesce_for_restore", bounded)
    prepared = []

    class Files:
        committed = False

        def __init__(self, *args):
            pass

        def prepare(self):
            prepared.append(1)

        def finish(self):
            pass

    monkeypatch.setattr(backup_files, "FileRestore", Files)
    monkeypatch.setattr(db_portable, "validate_sqlite_backup", lambda *a, **kw: None)
    monkeypatch.setattr(backup_files, "validate_staged_queue_spool", lambda *a: None)
    monkeypatch.setattr(obico_history_files, "validate_history", lambda *a: None)
    monkeypatch.setattr(farm_startup_guard, "hold", lambda *a: None)
    monkeypatch.setattr("backend.app.services.connector_runtime.connector_runtime.clear_restore_fence", lambda: None)
    source = tmp_path / "restore-source.db"
    with closing(sqlite3.connect(source)) as db:
        db.executescript(
            "CREATE TABLE printers(id INTEGER); CREATE TABLE settings(key TEXT,value TEXT);"
            "CREATE TABLE _migrations(version INTEGER); INSERT INTO _migrations VALUES(41);"
        )
    body = BytesIO()
    with ZipFile(body, "w") as archive:
        archive.writestr("bamdude.db", source.read_bytes())
    try:
        await asyncio.wait_for(owner_ready.wait(), 5)
        response = await async_client.post(
            "/api/v1/settings/restore", files={"file": ("backup.zip", body.getvalue(), "application/zip")}
        )
        assert response.status_code == 409, response.text
        assert "Spoolman delete or charge work has not stopped" in response.text
        assert not prepared, "nothing was prepared before the refusal"
        assert not task.done(), "the charge still runs: the restore waited for it, it did not cut it"
    finally:
        release.set()
        await asyncio.wait_for(task, 5)
    assert spoolman_source.charges_open() == 0


# --- A06: a manual assignment's remote proof belongs to one source ----------------------------


@pytest.mark.parametrize("change", ["mode_off_then_new_address", "new_address", "rotate", "mode_off"])
async def test_a_manual_assignment_refuses_a_proof_its_source_outlived(
    async_client, db_session, printer_factory, monkeypatch, change
):
    """The GET proved the number on A; a change of the source while it ran refuses the assignment, retryably."""
    from backend.app.api.routes import spoolman_inventory as route
    from backend.app.api.routes.settings import update_spoolman_settings
    from backend.app.api.routes.spoolman_usage import rotate_spoolman_usage_source
    from backend.app.core import database
    from backend.app.models.archive import PrintArchive
    from backend.app.services import print_usage_journal as journal

    printer = await printer_factory()
    await _seed_source(db_session)
    if change == "mode_off_then_new_address":
        # Spoolman off and nothing else proves accounting: the base may change under the GET.
        db_session.add(
            PrintArchive(
                printer_id=printer.id, status="printing", filename="a.3mf", print_name="a", file_path="", file_size=0
            )
        )
        await db_session.commit()
    got, deliver = asyncio.Event(), asyncio.Event()
    held_facts = []

    class LocalA:
        base_url = A
        health_check = AsyncMock(return_value=True)
        get_spools = AsyncMock(return_value=[])

        async def get_spool(self, spool_id):
            got.set()
            await deliver.wait()
            return {"id": spool_id, "filament": {}, "extra": {}, "archived": False}

    class HeldJournal:
        def journal_held(self):
            return True

        def note_held_binding_change(self, generation, tray, outgoing, incoming, *, stamp):
            held_facts.append((tray, incoming, stamp))
            return True

    monkeypatch.setattr(route, "get_spoolman_client", AsyncMock(return_value=LocalA()))
    monkeypatch.setattr(route, "_health_check_cache", {})
    monkeypatch.setattr(journal, "_observed_print", lambda _: (HeldJournal(), 1, None))
    monkeypatch.setattr(
        "backend.app.services.location_service.maybe_sync_spoolman_locations", AsyncMock(return_value=0)
    )
    assign = asyncio.create_task(
        async_client.post(
            "/api/v1/spoolman/inventory/slot-assignments",
            json={"printer_id": printer.id, "ams_id": 0, "tray_id": 0, "spoolman_spool_id": 17},
        )
    )
    try:
        await asyncio.wait_for(got.wait(), 5)
        if change in ("mode_off_then_new_address", "mode_off"):
            async with database.async_session() as other:
                await update_spoolman_settings({"spoolman_enabled": "false"}, db=other, _=None)
        if change in ("mode_off_then_new_address", "new_address"):
            async with database.async_session() as other:
                await update_spoolman_settings({"spoolman_url": B}, db=other, _=None)
        if change == "rotate":
            async with database.async_session() as other:
                await rotate_spoolman_usage_source(db=other, _=None)
    finally:
        deliver.set()
        response = await asyncio.wait_for(assign, 10)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "spoolman_source_changed"
    assert (await db_session.execute(select(SpoolmanSlotAssignment))).scalars().all() == []
    assert held_facts == [], "no journal fact for a number proved on the source that is gone"


async def test_a_manual_assignment_under_an_unchanged_source_stamps_its_proof(
    async_client, db_session, printer_factory, monkeypatch
):
    from backend.app.api.routes import spoolman_inventory as route
    from backend.app.services import print_usage_journal as journal, spoolman_source

    printer = await printer_factory()
    await _seed_source(db_session)
    held_facts = []

    class LocalA:
        base_url = A
        health_check = AsyncMock(return_value=True)
        get_spools = AsyncMock(return_value=[])

        async def get_spool(self, spool_id):
            return {"id": spool_id, "filament": {}, "extra": {}, "archived": False}

    class HeldJournal:
        def journal_held(self):
            return True

        def note_held_binding_change(self, generation, tray, outgoing, incoming, *, stamp):
            held_facts.append((tray, incoming, stamp))
            return True

    monkeypatch.setattr(route, "get_spoolman_client", AsyncMock(return_value=LocalA()))
    monkeypatch.setattr(route, "_health_check_cache", {})
    monkeypatch.setattr(journal, "_observed_print", lambda _: (HeldJournal(), 1, None))
    monkeypatch.setattr(
        "backend.app.services.location_service.maybe_sync_spoolman_locations", AsyncMock(return_value=0)
    )
    response = await async_client.post(
        "/api/v1/spoolman/inventory/slot-assignments",
        json={"printer_id": printer.id, "ams_id": 0, "tray_id": 0, "spoolman_spool_id": 17},
    )
    assert response.status_code == 200, response.text
    assert held_facts == [(0, (None, 17), spoolman_source.base_generation())]


# --- A08: a change of the mode waits for the slot-ledger writers past their check -------------


def _assignment_doubles(monkeypatch):
    """The Spoolman manual assignment's remote and journal as local fakes; returns its held facts."""
    from backend.app.api.routes import spoolman_inventory as route
    from backend.app.services import print_usage_journal as journal

    held_facts = []

    class LocalA:
        base_url = A
        health_check = AsyncMock(return_value=True)
        get_spools = AsyncMock(return_value=[])
        get_spool = AsyncMock(return_value={"id": 17, "filament": {}, "extra": {}, "archived": False})

    class HeldJournal:
        def journal_held(self):
            return True

        def note_held_binding_change(self, generation, tray, outgoing, incoming, *, stamp):
            held_facts.append((tray, incoming))
            return True

    monkeypatch.setattr(route, "get_spoolman_client", AsyncMock(return_value=LocalA()))
    monkeypatch.setattr(route, "_health_check_cache", {})
    monkeypatch.setattr(route.printer_manager, "get_client", lambda *_: None)
    monkeypatch.setattr(journal, "_observed_print", lambda _: (HeldJournal(), 1, None))
    monkeypatch.setattr(
        "backend.app.services.location_service.maybe_sync_spoolman_locations", AsyncMock(return_value=0)
    )
    return held_facts


async def _switch(enabled: str):
    from backend.app.api.routes.settings import update_spoolman_settings
    from backend.app.core import database

    async with database.async_session() as other:
        return await update_spoolman_settings({"spoolman_enabled": enabled}, db=other, _=None)


async def _observe(model, column):
    from backend.app.core import database

    async with database.async_session() as observer:
        mode = await observer.scalar(select(Settings.value).where(Settings.key == "spoolman_enabled"))
        return mode, (await observer.scalars(select(column))).all()


async def _until(predicate, timeout=8):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


async def test_a_switch_off_waits_for_a_manual_assignment_past_its_source_check(
    async_client, db_session, printer_factory, monkeypatch
):
    """Codex's A06/A07 probe: the switch came between the A06 check and the lane and the row came back."""
    from backend.app import main
    from backend.app.services import spoolman_source, spoolman_source_changes

    printer = await printer_factory()
    await _seed_source(db_session)
    held_facts = _assignment_doubles(monkeypatch)
    proved = asyncio.Event()
    prior = spoolman_source_changes.require_unchanged

    def observed(snapshot):
        prior(snapshot)
        proved.set()

    monkeypatch.setattr(spoolman_source_changes, "require_unchanged", observed)
    async with main._get_ams_assignment_lock(printer.id):
        assign = asyncio.create_task(
            async_client.post(
                "/api/v1/spoolman/inventory/slot-assignments",
                json={"printer_id": printer.id, "ams_id": 0, "tray_id": 0, "spoolman_spool_id": 17},
            )
        )
        await asyncio.wait_for(proved.wait(), 8)
        switch = asyncio.create_task(_switch("false"))
        await asyncio.sleep(0.2)
        assert spoolman_source.mode_change_in_flight()
        assert not switch.done() and not assign.done(), "the switch waits for the assignment past its check"
    response = await asyncio.wait_for(assign, 10)
    assert response.status_code == 200, response.text
    await asyncio.wait_for(switch, 10)
    assert await _observe(SpoolmanSlotAssignment, SpoolmanSlotAssignment.spoolman_spool_id) == ("false", [])
    assert held_facts == [(0, (None, 17))], "the fact was taken while Spoolman was the inventory"


def _built_in_spool():
    from backend.app.models.spool import Spool

    return Spool(material="PLA", subtype="Basic", brand="Generic", rgba="FF0000FF", label_weight=1000, weight_used=0)


async def test_a_switch_to_spoolman_waits_for_a_built_in_assignment_past_its_check(
    async_client, db_session, printer_factory, monkeypatch
):
    from unittest.mock import MagicMock, patch

    from backend.app import main
    from backend.app.models.spool_assignment import SpoolAssignment
    from backend.app.services import spoolman_activity

    printer = await printer_factory()
    db_session.add_all([Settings(key="spoolman_enabled", value="false"), Settings(key="spoolman_url", value=A)])
    spool = _built_in_spool()
    db_session.add(spool)
    await db_session.commit()
    monkeypatch.setattr(
        "backend.app.services.location_service.maybe_sync_spoolman_locations", AsyncMock(return_value=0)
    )
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = MagicMock()
        pm.get_status.return_value = None
        pm.ensure_fresh_connection = AsyncMock()
        async with main._get_ams_assignment_lock(printer.id):
            assign = asyncio.create_task(
                async_client.post(
                    "/api/v1/inventory/assignments",
                    json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 0, "tray_id": 1},
                )
            )
            await _until(spoolman_activity.slot_writes_open)
            switch = asyncio.create_task(_switch("true"))
            await asyncio.sleep(0.2)
            assert not switch.done() and not assign.done()
        response = await asyncio.wait_for(assign, 10)
        await asyncio.wait_for(switch, 10)
    assert response.status_code == 200, response.text
    assert await _observe(SpoolAssignment, SpoolAssignment.spool_id) == ("true", [])


async def test_a_writer_that_arrives_during_the_switch_asks_the_mode_again(
    async_client, db_session, printer_factory, monkeypatch
):
    from unittest.mock import MagicMock, patch

    from backend.app import main
    from backend.app.models.spool_assignment import SpoolAssignment
    from backend.app.services import spoolman_activity, spoolman_source

    printer = await printer_factory()
    db_session.add_all([Settings(key="spoolman_enabled", value="false"), Settings(key="spoolman_url", value=A)])
    spool = _built_in_spool()
    db_session.add(spool)
    await db_session.commit()
    monkeypatch.setattr(
        "backend.app.services.location_service.maybe_sync_spoolman_locations", AsyncMock(return_value=0)
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def in_flight():
        async with spoolman_activity.slot_write("ams_change_rfid"):
            entered.set()
            await release.wait()

    holder = asyncio.create_task(in_flight())
    await asyncio.wait_for(entered.wait(), 2)
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = MagicMock()
        pm.get_status.return_value = None
        pm.ensure_fresh_connection = AsyncMock()
        switch = asyncio.create_task(_switch("true"))
        await _until(spoolman_source.mode_change_in_flight)
        assign = asyncio.create_task(
            async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": printer.id, "ams_id": 0, "tray_id": 1},
            )
        )
        await asyncio.sleep(0.2)
        assert not assign.done()
        assert not main._get_ams_assignment_lock(printer.id).locked(), "it waits before the lane"
        release.set()
        await asyncio.wait_for(asyncio.gather(holder, switch), 10)
        response = await asyncio.wait_for(assign, 10)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "spoolman_mode"
    assert await _observe(SpoolAssignment, SpoolAssignment.spool_id) == ("true", [])


@pytest.mark.parametrize("on", [True, False], ids=["to-spoolman", "to-built-in"])
async def test_the_general_settings_save_empties_the_ledger_it_leaves(async_client, db_session, printer_factory, on):
    """Only the Spoolman settings route cleared it; the general save and Git left the old mode's rows."""
    from backend.app.models.spool_assignment import SpoolAssignment

    printer = await printer_factory()
    db_session.add_all(
        [Settings(key="spoolman_enabled", value="false" if on else "true"), Settings(key="spoolman_url", value=A)]
    )
    spool = _built_in_spool()
    db_session.add(spool)
    await db_session.commit()
    if on:
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=0, tray_id=1))
        model, column = SpoolAssignment, SpoolAssignment.spool_id
    else:
        db_session.add(SpoolmanSlotAssignment(printer_id=printer.id, ams_id=0, tray_id=1, spoolman_spool_id=17))
        model, column = SpoolmanSlotAssignment, SpoolmanSlotAssignment.spoolman_spool_id
    await db_session.commit()

    response = await async_client.put("/api/v1/settings/", json={"spoolman_enabled": on})

    assert response.status_code == 200, response.text
    assert await _observe(model, column) == ("true" if on else "false", [])


def _switching_off_once():
    """A remote call during which Spoolman is switched off — the door's check is already behind it."""
    done = []

    async def switch_off():
        if not done:
            done.append(True)
            await _switch("false")

    return switch_off


async def test_a_link_switched_off_meanwhile_saves_no_slot_row(async_client, db_session, printer_factory, monkeypatch):
    from backend.tests.integration.test_spoolman_link_announces import TAG as LINK_TAG, _client, _post

    printer = await printer_factory()
    await _seed_source(db_session)
    monkeypatch.setattr(
        "backend.app.services.location_service.maybe_sync_spoolman_locations", AsyncMock(return_value=0)
    )
    client = _client()
    switch_off = _switching_off_once()

    async def merge(*_args, **_kwargs):
        await switch_off()

    client.merge_spool_extra = AsyncMock(side_effect=merge)
    response, _broadcast = await _post(
        async_client,
        "/api/v1/spoolman/spools/11/link",
        client,
        {"spool_tag": LINK_TAG, "printer_id": printer.id, "ams_id": 0, "tray_id": 1},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "spoolman_source_changed"
    client.take_back_native_tags.assert_awaited_once()
    assert await _observe(SpoolmanSlotAssignment, SpoolmanSlotAssignment.spoolman_spool_id) == ("false", [])


async def test_a_from_slot_switched_off_meanwhile_saves_no_slot_row(
    async_client, db_session, printer_factory, monkeypatch
):
    from backend.app.api.routes import spoolman as routes

    printer = await printer_factory()
    await _seed_source(db_session)
    monkeypatch.setattr(
        "backend.app.services.location_service.maybe_sync_spoolman_locations", AsyncMock(return_value=0)
    )
    client, _native, _posted, _deliver = _creating_client(monkeypatch, {0: _tray(0, TAG)})
    switch_off = _switching_off_once()

    async def created(*_args, **_kwargs):
        await switch_off()
        return {"id": 17}

    monkeypatch.setattr(client, "sync_ams_tray", created)
    monkeypatch.setattr(routes, "get_spoolman_client", AsyncMock(return_value=client))
    monkeypatch.setattr(routes.printer_manager, "get_status", lambda *_: _printer_state([0]))
    response = await async_client.post(
        "/api/v1/spoolman/spools/from-slot", json={"printer_id": printer.id, "ams_id": 0, "tray_id": 0}
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "spoolman_source_changed"
    assert await _observe(SpoolmanSlotAssignment, SpoolmanSlotAssignment.spoolman_spool_id) == ("false", [])


async def test_a_sync_switched_off_meanwhile_saves_no_slot_row(async_client, db_session, printer_factory, monkeypatch):
    from backend.app.api.routes import spoolman as routes
    from backend.app.services.spoolman import ArchivedTagIndex

    printer = await printer_factory()
    await _seed_source(db_session)
    monkeypatch.setattr(
        "backend.app.services.location_service.maybe_sync_spoolman_locations", AsyncMock(return_value=0)
    )
    client, _native, _posted, _deliver = _creating_client(monkeypatch, {0: _tray(0, TAG)})
    switch_off = _switching_off_once()

    async def found(*_args, **_kwargs):
        await switch_off()
        return {"id": 18}

    monkeypatch.setattr(client, "sync_ams_tray", found)
    monkeypatch.setattr(ArchivedTagIndex, "holds_archived_reel", AsyncMock(return_value=False))
    monkeypatch.setattr(routes, "get_spoolman_client", AsyncMock(return_value=client))
    monkeypatch.setattr(routes.printer_manager, "get_status", lambda *_: _printer_state([0]))
    response = await async_client.post(f"/api/v1/spoolman/sync/{printer.id}")
    assert response.status_code == 200, response.text
    assert "Spoolman was turned off during the sync: slot assignments were not saved" in response.json()["errors"]
    assert await _observe(SpoolmanSlotAssignment, SpoolmanSlotAssignment.spoolman_spool_id) == ("false", [])
