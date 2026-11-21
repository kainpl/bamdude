"""The operator's swap declaration under two real PostgreSQL sessions (swap-source-declaration plan T9.6, 5-9).

Run by ``postgres_scenario_runner`` in its own interpreter (``declare:<name>``) against the dedicated scratch server.
Every entry is the production one -- ``inspect`` / ``declare``, the library scan, ``refresh`` of selected jobs,
AutoQueue's ``_assign``, the change_table claim -- with the process-local asyncio locks replaced by no-ops, so what
serializes two sessions is PostgreSQL's own advisory and row locks, as between two processes. One side is held at a
named point until PostgreSQL shows the other waiting on a lock, then let go. A deadlock, an error or a timeout is
reported as such, never folded into "ok".
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select, text

from backend.tests.integration.swap_claim_scenarios import _outcome, _process_locks_off, _waiting_on_a_lock

PLATE = {15: [{"id": 3, "type": "PLA", "color": "#FF0000", "used_g": "0.0001"}]}


def _data_dir() -> Path:
    from backend.app.core.config import settings

    return Path(settings.base_dir)


def _bytes(path: Path, tag: str) -> bytes:
    import zipfile

    from backend.tests.fixtures.filament_routing_cases import write_routing_3mf

    path.parent.mkdir(parents=True, exist_ok=True)
    write_routing_3mf(path, PLATE)
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("Metadata/tag.txt", tag)
    return path.read_bytes()


def _sha(raw: bytes) -> str:
    import hashlib

    return hashlib.sha256(raw).hexdigest()


def _later(path: Path) -> None:
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000_000))


async def _user():
    from backend.app.core.database import async_session
    from backend.app.models.user import User
    from backend.tests.auth_helpers import mint_test_session

    async with async_session() as db:
        name = f"op-{uuid.uuid4().hex[:6]}"
        user = User(username=name, email=f"{name}@example.com", password_hash="x", role="admin", is_active=True)
        db.add(user)
        await db.flush()
        await mint_test_session(db, user=user)
        return user


def _actor(user):
    from backend.app.services import swap_source_declarations as decl

    return decl.Actor(kind="user", user_id=user.id, label=user.username, capture=user._auth_capture)


def _rights():
    from backend.app.services import swap_source_declarations as decl

    return decl.Rights(read_all=True, update_all=True)


def _job_rights():
    from backend.app.services import swap_job_refresh as jobs

    return jobs.JobRights(read_all=True, update_all=True)


async def _archive(path: Path, owner) -> int:
    from backend.app.core.database import async_session
    from backend.app.models.archive import PrintArchive

    raw = path.read_bytes()
    async with async_session() as db:
        row = PrintArchive(
            filename=path.name,
            file_path=str(path),
            file_size=len(raw),
            status="completed",
            source_content_hash=_sha(raw),
            content_hash=_sha(raw),
            created_by_id=owner.id,
        )
        db.add(row)
        await db.commit()
        return row.id


async def _library(path: Path, owner, *, external: bool = False) -> int:
    from backend.app.core.database import async_session
    from backend.app.models.library import LibraryFile

    raw = path.read_bytes()
    async with async_session() as db:
        row = LibraryFile(
            filename=path.name,
            file_path=str(path),
            file_type="3mf",
            file_size=len(raw),
            file_hash=_sha(raw),
            is_external=external,
            swap_compatible=False,
            created_by_id=owner.id,
        )
        db.add(row)
        await db.commit()
        return row.id


async def _declare(kind: str, source_id: int, seen: dict, user, value: bool) -> str:
    from fastapi import HTTPException

    from backend.app.services import swap_source_declarations as decl

    try:
        await asyncio.wait_for(
            decl.declare(
                kind,
                source_id,
                value=value,
                expected_sha256=seen["sha256"],
                expected_revision=seen["revision"],
                operation_id=str(uuid.uuid4()),
                actor=_actor(user),
                rights=_rights(),
            ),
            60,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        return f"{exc.status_code}:{detail.get('error', '')}"
    except Exception as exc:  # noqa: BLE001 — reported
        from backend.tests.integration.lock_barriers import sqlstate

        return "deadlock" if sqlstate(exc) == "40P01" else f"error:{type(exc).__name__}:{exc}"
    return "ok"


async def _refresh_outcome(coro) -> str:
    from fastapi import HTTPException

    try:
        await asyncio.wait_for(coro, 60)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        return f"{exc.status_code}:{detail.get('error', '')}"
    except Exception as exc:  # noqa: BLE001 — reported
        from backend.tests.integration.lock_barriers import sqlstate

        return "deadlock" if sqlstate(exc) == "40P01" else f"error:{type(exc).__name__}:{exc}"
    return "ok"


async def _until_waiting(minimum: int = 1) -> int:
    """Poll PostgreSQL until ``minimum`` sessions wait on a lock (or give up): the evidence the other side waits."""
    seen = 0
    for _ in range(400):
        seen = await _waiting_on_a_lock()
        if seen >= minimum:
            return seen
        await asyncio.sleep(0.025)
    return seen


async def _audits(kind: str, source_id: int) -> list[str]:
    from backend.app.core.database import async_session
    from backend.app.models.swap_source_declaration_audit import SwapSourceDeclarationAudit as Audit

    async with async_session() as db:
        rows = (
            await db.execute(
                select(Audit.action).where(Audit.source_kind == kind, Audit.source_id == source_id).order_by(Audit.id)
            )
        ).scalars()
        return list(rows)


async def _source_state(kind: str, source_id: int) -> dict:
    from backend.app.core.database import async_session
    from backend.app.models.archive import PrintArchive
    from backend.app.models.library import LibraryFile
    from backend.app.services import swap_compatibility as sc

    async with async_session() as db:
        row = await db.get(PrintArchive if kind == "archive" else LibraryFile, source_id)
        declaration = sc.current_declaration(row)
        return {
            "effective": sc.source_effective(row),
            "origin": declaration.origin if declaration else None,
            "hash": getattr(row, "file_hash", None),
            "swap_tag": "swap" in (getattr(row, "file_tags", None) or []),
        }


# --- 5. two declarations of one revision -----------------------------------------------------------------------------


async def scenario_two_declares() -> dict:
    """Two operators declare one source from the same inspected revision: one commit with its audit line, the other
    refused -- on an archive (its write guard) and on a library row (its row lock)."""
    from backend.app.services import swap_source_declarations as decl

    _process_locks_off()
    user = await _user()
    base = _data_dir()
    _bytes(base / "src" / "archived.gcode.3mf", "archived")
    _bytes(base / "src" / "kept.gcode.3mf", "kept")
    sources = {
        "archive": await _archive(base / "src" / "archived.gcode.3mf", user),
        "library": await _library(base / "src" / "kept.gcode.3mf", user),
    }
    report: dict = {}
    real = decl._fresh_rights
    for kind, source_id in sources.items():
        seen = await decl.inspect(kind, source_id, actor=_actor(user), rights=_rights())
        calls = 0
        waited: list[int] = []

        async def rights(db, k, actor, *, _waited=waited):
            nonlocal calls
            calls += 1
            if calls == 1:  # the first writer holds its lock until PostgreSQL shows the second waiting
                _waited.append(await _until_waiting())
            return await real(db, k, actor)

        decl._fresh_rights = rights
        try:
            outcomes = await asyncio.gather(*[_declare(kind, source_id, seen, user, value) for value in (True, False)])
        finally:
            decl._fresh_rights = real
        report[kind] = {
            "outcomes": sorted(outcomes),
            "waited": max(waited or [0]),
            "audit": await _audits(kind, source_id),
            **await _source_state(kind, source_id),
        }
    return report


# --- 6. a scan and a declaration of one library row ------------------------------------------------------------------


async def _scan(folder_id: int) -> str:
    from backend.app.core.database import async_session
    from backend.app.models.library_scan import LibraryScanJob
    from backend.app.services.library_scan import run_scan

    async with async_session() as db:
        job = LibraryScanJob(folder_id=folder_id, status="queued")
        db.add(job)
        await db.commit()
        job_id = job.id
    outcome = await _outcome(run_scan(job_id))
    async with async_session() as db:
        job = await db.get(LibraryScanJob, job_id)
        return outcome if job.status == "finished" else f"{outcome}/{job.status}:{job.error}"


def _inprocess_library_worker() -> None:
    """The scan's file worker, in process (as the test suite supplies it): the scan's DATABASE writes and locks are
    what these scenarios watch; the worker process is a separate concern."""
    import uuid as _uuid

    from backend.app.library_file_service import _walk
    from backend.app.services import library_file_runtime
    from backend.app.services.library_file_preparation import hash_file, prepare_file

    class Worker:
        def __init__(self):
            self.walks = {}

        def health(self):
            return {"state": "unavailable", "reason": "scenario_adapter"}

        async def prepare(self, path, *, root, filename=None):
            return await asyncio.to_thread(prepare_file, path, root=root, display_filename=filename)

        async def hash(self, path, *, root):
            return await asyncio.to_thread(hash_file, path, root=root)

        async def walk_start(self, root, show_hidden):
            token = _uuid.uuid4().hex
            self.walks[token] = _walk(root, show_hidden)
            return token

        async def walk_next(self, root, token):
            def page():
                entries = []
                for _ in range(32):
                    try:
                        entries.append(next(self.walks[token]))
                    except StopIteration:
                        return {"entries": entries, "done": True}
                return {"entries": entries, "done": False}

            result = await asyncio.to_thread(page)
            if result["done"]:
                self.walks.pop(token)
            return result

        async def walk_end(self, root, token):
            self.walks.pop(token, None)

        async def present(self, root, paths):
            return await asyncio.to_thread(lambda: [Path(path).exists() for path in paths])

    library_file_runtime.runtime = Worker()


async def _share(name: str):
    from backend.app.core.database import async_session
    from backend.app.models.library import LibraryFile, LibraryFolder

    root = _data_dir() / name
    root.mkdir(parents=True, exist_ok=True)
    async with async_session() as db:
        folder = LibraryFolder(name=name, is_external=True, external_path=str(root), external_show_hidden=False)
        db.add(folder)
        await db.commit()
        folder_id = folder.id
    _bytes(root / "plate.gcode.3mf", f"{name}-v1")
    assert await _scan(folder_id) == "ok"
    async with async_session() as db:
        row_id = (
            await db.execute(select(LibraryFile.id).where(LibraryFile.file_path == str(root / "plate.gcode.3mf")))
        ).scalar_one()
    return root / "plate.gcode.3mf", folder_id, row_id


async def scenario_scan_vs_declare() -> dict:
    """Declaration holding the row, scan of NEW bytes waiting: the scan writes the new hash with a classifier carrier,
    the old bytes' declaration is not carried. Declaration holding the row, scan of the SAME bytes waiting: the
    operator's declaration survives. Scan holding the row with new bytes, the old bytes' declaration waiting: it is
    refused, nothing of it written. Tags follow the effective fact after every commit."""
    from backend.app.services import library_helpers, swap_source_declarations as decl

    _process_locks_off()
    _inprocess_library_worker()
    user = await _user()
    report: dict = {}
    real_rights = decl._fresh_rights

    async def declaration_holds_then(scan_change, name):
        path, folder_id, row_id = await _share(name)
        seen = await decl.inspect("library", row_id, actor=_actor(user), rights=_rights())
        waited: list[int] = []
        scanning: list = []

        async def rights(db, k, actor):
            scan_change(path)
            scanning.append(asyncio.ensure_future(_scan(folder_id)))
            waited.append(await _until_waiting())
            return await real_rights(db, k, actor)

        decl._fresh_rights = rights
        try:
            declared = await _declare("library", row_id, seen, user, True)
        finally:
            decl._fresh_rights = real_rights
        scanned = await scanning[0]
        return {
            "declared": declared,
            "scanned": scanned,
            "waited": max(waited or [0]),
            "audit": await _audits("library", row_id),
            **await _source_state("library", row_id),
        }

    def new_bytes(path):
        _bytes(path, "new-bytes")
        _later(path)

    def same_bytes(path):
        _later(path)  # the same bytes looked at again; the scan re-hashes and keeps what the row declares

    report["declaration_first_new_bytes"] = await declaration_holds_then(new_bytes, "nas-a")
    report["declaration_first_same_bytes"] = await declaration_holds_then(same_bytes, "nas-b")

    # The scan holds the row (new bytes written, before its commit); the old bytes' declaration waits.
    path, folder_id, row_id = await _share("nas-c")
    seen = await decl.inspect("library", row_id, actor=_actor(user), rights=_rights())
    real_write = decl._write
    verified = asyncio.Event()
    release = asyncio.Event()

    async def write_when_released(*args, **kwargs):
        verified.set()
        await asyncio.wait_for(release.wait(), 30)
        return await real_write(*args, **kwargs)

    decl._write = write_when_released
    real_tags = library_helpers.sync_system_tags
    scan_holds = asyncio.Event()
    waited: list[int] = []

    async def tags_while_the_declaration_waits(db, row):
        if not scan_holds.is_set():
            scan_holds.set()
            release.set()  # the declaration now goes for the row the scan holds
            waited.append(await _until_waiting())
        return await real_tags(db, row)

    try:
        declaring = asyncio.ensure_future(_declare("library", row_id, seen, user, True))
        await asyncio.wait_for(verified.wait(), 30)
        new_bytes(path)
        library_helpers.sync_system_tags = tags_while_the_declaration_waits
        scanned = await _scan(folder_id)
        declared = await declaring
    finally:
        decl._write = real_write
        library_helpers.sync_system_tags = real_tags
    report["scan_first_new_bytes"] = {
        "declared": declared,
        "scanned": scanned,
        "waited": max(waited or [0]),
        "audit": await _audits("library", row_id),
        **await _source_state("library", row_id),
    }
    return report


# --- 7. a refresh against AutoQueue's assignment and a printer claim -------------------------------------------------


async def _routing_farm(user):
    """A library source the router can place (a P1P with an external PLA feed), declared True by the operator after
    two jobs captured it unknown: one on the printer's own queue, one in the AutoQueue."""
    from unittest.mock import MagicMock

    from backend.app.core.database import async_session
    from backend.app.models.auto_queue import AutoQueueItem
    from backend.app.models.library import LibraryFile
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer import Printer
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.models.queue_source import QueueSource
    from backend.app.services import queue_sources, swap_compatibility as sc, swap_source_declarations as decl
    from backend.app.services.bambu_mqtt import BambuMQTTClient
    from backend.app.services.printer_manager import printer_manager
    from backend.app.services.queue_source_capture import capture_staged, plan_capture

    path = _data_dir() / "library" / f"part-{uuid.uuid4().hex[:6]}.gcode.3mf"
    _bytes(path, "part")
    library_id = await _library(path, user)
    async with async_session() as db:
        printer = Printer(
            name="router-p1p",
            serial_number=f"ROUTER{uuid.uuid4().hex[:6]}",
            ip_address="192.0.2.41",
            access_code="00000000",
            model="P1P",
            is_active=True,
        )
        db.add(printer)
        await db.flush()
        db.add(PrinterQueue(id=printer.id, printer_id=printer.id, status="idle", auto_distribute_eligible=True))
        # The printer job waits on another printer's queue: a pending job on the router's own printer would make the
        # assignment refuse that printer as busy, before any lock is reached.
        other = Printer(
            name="other-p1p",
            serial_number=f"OTHER{uuid.uuid4().hex[:6]}",
            ip_address="192.0.2.42",
            access_code="00000000",
            model="P1P",
        )
        db.add(other)
        await db.flush()
        db.add(PrinterQueue(id=other.id, printer_id=other.id, status="idle", auto_distribute_eligible=False))
        await db.commit()
        printer_id, other_id = printer.id, other.id
    mqtt = BambuMQTTClient("127.0.0.1", "SYNTHETIC", "00000000", model="P1P")
    mqtt._client = MagicMock()
    mqtt.state.connected = True
    mqtt.state.state = "IDLE"
    mqtt._process_message(
        {
            "print": {
                "command": "push_status",
                "ams": {"ams": []},
                "vt_tray": {"id": 254, "tray_type": "PLA", "tray_color": "0000FF"},
            }
        }
    )
    printer_manager._clients[printer_id] = mqtt
    printer_manager._models[printer_id] = "P1P"

    async with async_session() as db:
        staged = await capture_staged(plan_capture(library_file=await db.get(LibraryFile, library_id)))
    made: dict = {}

    async def attach(session, source: QueueSource) -> None:
        snapshot = sc.snapshot_with_fact(queue_sources.snapshot_for(staged.receipt, source), sc.UNKNOWN_SOURCE)
        queued = PrintQueueItem(
            queue_id=other_id,
            position=1,
            status="pending",
            manual_start=True,
            library_file_id=library_id,
            queue_source_id=source.id,
            source_snapshot=dict(snapshot),
            created_by_id=user.id,
        )
        routed = AutoQueueItem(
            status="pending",
            library_file_id=library_id,
            queue_source_id=source.id,
            source_snapshot=dict(snapshot),
            created_by_id=user.id,
        )
        session.add_all([queued, routed])
        await session.flush()
        made.update(queue=queued.id, auto=routed.id)

    await queue_sources.publish(staged.receipt, attach, session_factory=async_session)
    seen = await decl.inspect("library", library_id, actor=_actor(user), rights=_rights())
    assert await _declare("library", library_id, seen, user, True) == "ok"
    return SimpleNamespace(printer_id=printer_id, library_id=library_id, **made)


async def _assign(farm) -> str:
    from backend.app.core.database import async_session
    from backend.app.models.auto_queue import AutoQueueItem
    from backend.app.models.printer import Printer
    from backend.app.services.auto_queue_scheduler import AutoPlacementConflict, AutoQueueScheduler

    async with async_session() as db:
        item = await db.get(AutoQueueItem, farm.auto)
        printer = await db.get(Printer, farm.printer_id)
        try:
            await asyncio.wait_for(AutoQueueScheduler()._assign(db, item, printer), 60)
            await db.commit()
        except AutoPlacementConflict as exc:
            await db.rollback()
            return f"conflict:{exc.code}"
        except Exception as exc:  # noqa: BLE001 — reported
            await db.rollback()
            from backend.tests.integration.lock_barriers import sqlstate

            return "deadlock" if sqlstate(exc) == "40P01" else f"error:{type(exc).__name__}:{exc}"
    return "ok"


async def _jobs_state(farm) -> dict:
    from backend.app.core.database import async_session
    from backend.app.models.auto_queue import AutoQueueItem
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services import swap_compatibility as sc

    async with async_session() as db:
        queued = await db.get(PrintQueueItem, farm.queue)
        routed = await db.get(AutoQueueItem, farm.auto)
        children = (
            (await db.execute(select(PrintQueueItem).where(PrintQueueItem.source_auto_item_id == farm.auto)))
            .scalars()
            .all()
        )
        return {
            "queue_fact": sc.snapshot_fact(queued.source_snapshot).value,
            "auto_fact": sc.snapshot_fact(routed.source_snapshot).value,
            "auto_status": routed.status,
            "children": [sc.snapshot_fact(child.source_snapshot).value for child in children],
        }


async def _refresh(farm, user, listing) -> str:
    from backend.app.services import swap_job_refresh as jobs

    picked = [
        jobs.JobRef(entry["kind"], entry["id"], entry["revision"])
        for entry in listing["jobs"]
        if (entry["kind"], entry["id"]) in {("queue", farm.queue), ("auto_queue", farm.auto)}
    ]
    return await _refresh_outcome(
        jobs.refresh(
            "library",
            farm.library_id,
            declaration_id=listing["declaration_id"],
            source_revision=listing["source_revision"],
            jobs=picked,
            operation_id=str(uuid.uuid4()),
            actor=_actor(user),
            rights=_rights(),
            job_rights=_job_rights(),
        )
    )


async def scenario_refresh_vs_assignment() -> dict:
    """Refresh first, holding its lane and both job rows: the assignment waits, then finds the basis it decided on
    changed and creates no child. Assignment first, holding its claim: the refresh waits, then finds the router row
    routed and changes nothing -- neither job, no audit line."""
    from backend.app.services import auto_queue_scheduler, swap_job_refresh as jobs

    _process_locks_off()
    user = await _user()
    report: dict = {}

    farm = await _routing_farm(user)
    listing = await jobs.candidates(
        "library", farm.library_id, actor=_actor(user), rights=_rights(), job_rights=_job_rights()
    )
    real_job_rights = jobs._fresh_job_rights
    waited: list[int] = []
    assigning: list = []

    async def job_rights_while_the_assignment_waits(db, actor, granted):
        assigning.append(asyncio.ensure_future(_assign(farm)))
        waited.append(await _until_waiting())
        return await real_job_rights(db, actor, granted)

    jobs._fresh_job_rights = job_rights_while_the_assignment_waits
    try:
        refreshed = await _refresh(farm, user, listing)
    finally:
        jobs._fresh_job_rights = real_job_rights
    assigned = await assigning[0]
    report["refresh_first"] = {
        "refreshed": refreshed,
        "assigned": assigned,
        "waited": max(waited or [0]),
        "audit": await _audits("library", farm.library_id),
        **await _jobs_state(farm),
    }

    farm = await _routing_farm(user)
    listing = await jobs.candidates(
        "library", farm.library_id, actor=_actor(user), rights=_rights(), job_rights=_job_rights()
    )
    real_holds = auto_queue_scheduler.swap_basis_holds
    waited = []
    refreshing: list = []

    async def holds_while_the_refresh_waits(db, item, basis):
        answer = await real_holds(db, item, basis)
        refreshing.append(asyncio.ensure_future(_refresh(farm, user, listing)))
        waited.append(await _until_waiting())
        return answer

    auto_queue_scheduler.swap_basis_holds = holds_while_the_refresh_waits
    try:
        assigned = await _assign(farm)
    finally:
        auto_queue_scheduler.swap_basis_holds = real_holds
    refreshed = await refreshing[0]
    report["assignment_first"] = {
        "assigned": assigned,
        "refreshed": refreshed,
        "waited": max(waited or [0]),
        "audit": await _audits("library", farm.library_id),
        **await _jobs_state(farm),
    }
    return report


# --- 8. a refresh against a move, a delete, a new declaration, a restore; no deadlock --------------------------------


async def _archive_farm(user, *, jobs_count: int = 1):
    """An archive declared True after ``jobs_count`` printer jobs captured it unknown, all on one printer."""
    from backend.app.core.database import async_session
    from backend.app.models.archive import PrintArchive
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer import Printer
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.models.queue_source import QueueSource
    from backend.app.services import queue_sources, swap_compatibility as sc, swap_source_declarations as decl
    from backend.app.services.queue_source_capture import capture_staged, plan_capture

    path = _data_dir() / "archive" / f"run-{uuid.uuid4().hex[:6]}.gcode.3mf"
    _bytes(path, path.name)
    archive_id = await _archive(path, user)
    async with async_session() as db:
        printers = []
        for _ in range(2):
            printer = Printer(
                name="refresh",
                serial_number=f"REFRESH{uuid.uuid4().hex[:6]}",
                ip_address="192.0.2.51",
                access_code="00000000",
                model="P1S",
            )
            db.add(printer)
            await db.flush()
            db.add(PrinterQueue(id=printer.id, printer_id=printer.id, status="idle"))
            printers.append(printer.id)
        await db.commit()
    async with async_session() as db:
        staged = await capture_staged(plan_capture(archive=await db.get(PrintArchive, archive_id)))
    made: list[int] = []

    async def attach(session, source: QueueSource) -> None:
        snapshot = sc.snapshot_with_fact(queue_sources.snapshot_for(staged.receipt, source), sc.UNKNOWN_SOURCE)
        for _ in range(jobs_count):
            row = PrintQueueItem(
                queue_id=printers[0],
                position=1,
                status="failed",
                archive_id=archive_id,
                queue_source_id=source.id,
                source_snapshot=dict(snapshot),
                created_by_id=user.id,
            )
            session.add(row)
            await session.flush()
            made.append(row.id)

    await queue_sources.publish(staged.receipt, attach, session_factory=async_session)
    seen = await decl.inspect("archive", archive_id, actor=_actor(user), rights=_rights())
    assert await _declare("archive", archive_id, seen, user, True) == "ok"
    return SimpleNamespace(archive_id=archive_id, printers=printers, jobs=made)


async def _refresh_archive(farm, user, listing, *, only=None) -> str:
    from backend.app.services import swap_job_refresh as jobs

    picked = [
        jobs.JobRef(entry["kind"], entry["id"], entry["revision"])
        for entry in listing["jobs"]
        if only is None or entry["id"] in only
    ]
    return await _refresh_outcome(
        jobs.refresh(
            "archive",
            farm.archive_id,
            declaration_id=listing["declaration_id"],
            source_revision=listing["source_revision"],
            jobs=picked,
            operation_id=str(uuid.uuid4()),
            actor=_actor(user),
            rights=_rights(),
            job_rights=_job_rights(),
        )
    )


async def _facts(job_ids) -> list:
    from backend.app.core.database import async_session
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services import swap_compatibility as sc

    async with async_session() as db:
        out = []
        for job_id in job_ids:
            row = await db.get(PrintQueueItem, job_id)
            out.append(None if row is None else sc.snapshot_fact(row.source_snapshot).value)
        return out


async def _held_row_change(job_id: int, change, refresh_coro) -> tuple[str, str, int]:
    """Another session takes the job's row and changes it; the refresh starts and waits on that row; then the change
    commits."""
    from backend.app.core.database import async_session
    from backend.app.models.print_queue import PrintQueueItem

    async def changer():
        async with async_session() as db:
            row = (
                await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == job_id).with_for_update())
            ).scalar_one()
            await change(db, row)
            refreshing.append(asyncio.ensure_future(refresh_coro))
            waited.append(await _until_waiting())
            await db.commit()

    refreshing: list = []
    waited: list[int] = []
    changed = await _outcome(changer())
    refreshed = await refreshing[0]
    return changed, refreshed, max(waited or [0])


async def scenario_refresh_vs_changes() -> dict:
    """A move or a delete holding the job's row, a new declaration holding the archive's guard, a storage restore in
    between: the refresh waits where PostgreSQL makes it wait and then refuses the whole group -- nothing written, no
    audit line. A refresh, a declaration and a change_table claim of one archive at once: no deadlock."""
    from backend.app.services import (
        queue_sources,
        swap_job_refresh as jobs,
        swap_source_declarations as decl,
        swap_terminal,
    )

    _process_locks_off()
    user = await _user()
    report: dict = {}

    async def listing_of(farm):
        return await jobs.candidates(
            "archive", farm.archive_id, actor=_actor(user), rights=_rights(), job_rights=_job_rights()
        )

    farm = await _archive_farm(user, jobs_count=2)
    listing = await listing_of(farm)

    async def move(db, row):
        row.queue_id = farm.printers[1]
        await db.flush()

    changed, refreshed, waited = await _held_row_change(farm.jobs[0], move, _refresh_archive(farm, user, listing))
    report["move"] = {
        "changed": changed,
        "refreshed": refreshed,
        "waited": waited,
        "facts": await _facts(farm.jobs),
        "audit": await _audits("archive", farm.archive_id),
    }

    farm = await _archive_farm(user, jobs_count=2)
    listing = await listing_of(farm)

    async def delete(db, row):
        await db.delete(row)
        await db.flush()

    changed, refreshed, waited = await _held_row_change(farm.jobs[0], delete, _refresh_archive(farm, user, listing))
    report["delete"] = {
        "changed": changed,
        "refreshed": refreshed,
        "waited": waited,
        "facts": await _facts(farm.jobs[1:]),
        "audit": await _audits("archive", farm.archive_id),
    }

    farm = await _archive_farm(user)
    listing = await listing_of(farm)
    again = await decl.inspect("archive", farm.archive_id, actor=_actor(user), rights=_rights())
    real_rights = decl._fresh_rights
    waited_list: list[int] = []
    refreshing: list = []

    async def rights_while_the_refresh_waits(db, k, actor):
        refreshing.append(asyncio.ensure_future(_refresh_archive(farm, user, listing)))
        waited_list.append(await _until_waiting())
        return await real_rights(db, k, actor)

    decl._fresh_rights = rights_while_the_refresh_waits
    try:
        redeclared = await _declare("archive", farm.archive_id, again, user, False)
    finally:
        decl._fresh_rights = real_rights
    refreshed = await refreshing[0]
    report["redeclare"] = {
        "redeclared": redeclared,
        "refreshed": refreshed,
        "waited": max(waited_list or [0]),
        "facts": await _facts(farm.jobs),
        "audit": await _audits("archive", farm.archive_id),
    }

    farm = await _archive_farm(user)
    listing = await listing_of(farm)
    queue_sources.invalidate_receipts()  # what a full restore does to the storage generation
    report["restore"] = {
        "refreshed": await _refresh_archive(farm, user, listing),
        "facts": await _facts(farm.jobs),
        "audit": await _audits("archive", farm.archive_id),
    }

    # No deadlock: the refresh holds its lane and its job rows; a declaration of the archive and a change_table claim
    # (the printer's lane, then the archive) start; everything finishes.
    farm = await _archive_farm(user)
    listing = await listing_of(farm)
    again = await decl.inspect("archive", farm.archive_id, actor=_actor(user), rights=_rights())
    from backend.app.core.database import async_session

    async with async_session() as db:
        from backend.app.models.archive import PrintArchive

        archive = await db.get(PrintArchive, farm.archive_id)
        archive.extra_data = {**(archive.extra_data or {}), "swap_macro_events_pending": ["swap_mode_change_table"]}
        await db.commit()
    real_lock_source = jobs._lock_source
    others: list = []
    waited_list = []

    async def lock_source_while_others_start(db, kind, source_id):
        others.append(asyncio.ensure_future(_declare("archive", farm.archive_id, again, user, False)))
        others.append(
            asyncio.ensure_future(
                _outcome(
                    swap_terminal.claim_change_table(
                        async_session, farm.archive_id, queue_id=farm.printers[0], owner="test", allow_absent=True
                    )
                )
            )
        )
        waited_list.append(await _until_waiting())
        return await real_lock_source(db, kind, source_id)

    jobs._lock_source = lock_source_while_others_start
    try:
        refreshed = await _refresh_archive(farm, user, listing)
    finally:
        jobs._lock_source = real_lock_source
    declared, claimed = [await task for task in others]
    swap_terminal._sending.discard(farm.archive_id)
    report["no_deadlock"] = {
        "refreshed": refreshed,
        "declared": declared,
        "claimed": claimed,
        "waited": max(waited_list or [0]),
    }
    return report


async def scenario_refresh_vs_source_delete() -> dict:
    """A hard delete of the source archive holds its write guard and is about to detach the jobs that name it, while a
    refresh of those jobs starts. The refresh takes the archive before the job rows -- the archive -> queue order every
    archive writer keeps -- so it waits behind the delete, and then finds no source: no deadlock, nothing written."""
    from backend.app.core.database import async_session
    from backend.app.services import queue_source_release, swap_job_refresh as jobs
    from backend.app.services.archive import ArchiveService

    _process_locks_off()
    user = await _user()
    farm = await _archive_farm(user, jobs_count=2)
    listing = await jobs.candidates(
        "archive", farm.archive_id, actor=_actor(user), rights=_rights(), job_rights=_job_rights()
    )
    real_purged = queue_source_release.source_purged
    refreshing: list = []
    waited: list[int] = []

    async def purged_once_the_refresh_waits(db, **kwargs):
        refreshing.append(asyncio.ensure_future(_refresh_archive(farm, user, listing)))
        waited.append(await _until_waiting())
        return await real_purged(db, **kwargs)

    queue_source_release.source_purged = purged_once_the_refresh_waits

    async def delete():
        async with async_session() as db:
            assert await ArchiveService(db).delete_archive(farm.archive_id)
            await db.commit()

    try:
        deleted = await _outcome(delete())
    finally:
        queue_source_release.source_purged = real_purged
    refreshed = await refreshing[0]
    return {
        "deleted": deleted,
        "refreshed": refreshed,
        "waited": max(waited or [0]),
        "facts": await _facts(farm.jobs),
        "audit": await _audits("archive", farm.archive_id),
    }


# --- T9.7: a key that loses its authority while waiting; one operation sent twice ---------------------------------------


def _storage_guard_off() -> None:
    """Two server processes do not share the in-process storage guard either: only PostgreSQL may serialize them."""
    from contextlib import asynccontextmanager

    from backend.app.services import queue_sources

    @asynccontextmanager
    async def none():
        yield

    queue_sources.storage_mutation = none


async def _key_for(farm, *, permissions: list[str]):
    """A non-admin owner whose group grants ``permissions``, and its API key limited to the farm's printer."""
    from backend.app.core.auth_lifetime import capture_key
    from backend.app.core.database import async_session
    from backend.app.models.api_key import APIKey
    from backend.app.models.group import Group
    from backend.app.models.user import User
    from backend.tests.auth_helpers import bind_test_key

    async with async_session() as db:
        name = f"key-{uuid.uuid4().hex[:6]}"
        group = Group(name=f"grp-{name}", permissions=permissions)
        owner = User(username=name, email=f"{name}@example.com", password_hash="x", role="user", is_active=True)
        owner.groups.append(group)
        db.add_all([group, owner])
        await db.flush()
        key = APIKey(
            name=name,
            key_hash=f"not-used-{name}",
            key_prefix="bd_test",
            user_id=owner.id,
            enabled=True,
            printer_ids=[farm.printers[0]],
            can_queue=True,
            can_read_status=True,
            can_manage_archives=True,
        )
        db.add(await bind_test_key(db, key, owner))
        await db.commit()
        return SimpleNamespace(owner_id=owner.id, key_id=key.id, group_id=group.id, capture=capture_key(key))


async def _key_revoked_while_waiting(revocation: str) -> dict:
    from backend.app.core.database import async_session
    from backend.app.models.api_key import APIKey
    from backend.app.models.group import Group
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services import swap_job_refresh as jobs, swap_source_declarations as decl

    user = await _user()
    farm = await _archive_farm(user)
    full = ["archives:read_all", "archives:update_all", "queue:read_all", "queue:update_all"]
    key = await _key_for(farm, permissions=full)
    actor = decl.Actor("api_key", key.owner_id, "review key", key.key_id, capture=key.capture)
    listing = await jobs.candidates("archive", farm.archive_id, actor=actor, rights=_rights(), job_rights=_job_rights())
    picked = [jobs.JobRef(entry["kind"], entry["id"], entry["revision"]) for entry in listing["jobs"]]

    async with async_session() as holder:  # another writer holds the job's row: the refresh waits on PostgreSQL
        await holder.execute(select(PrintQueueItem).where(PrintQueueItem.id == farm.jobs[0]).with_for_update())
        refreshing = asyncio.ensure_future(
            _refresh_outcome(
                jobs.refresh(
                    "archive",
                    farm.archive_id,
                    declaration_id=listing["declaration_id"],
                    source_revision=listing["source_revision"],
                    jobs=picked,
                    operation_id=str(uuid.uuid4()),
                    actor=actor,
                    rights=_rights(),
                    job_rights=_job_rights(),
                    printer_scope=frozenset({farm.printers[0]}),
                )
            )
        )
        waited = await _until_waiting()
        async with async_session() as admin:  # the authority goes while the refresh waits
            if revocation == "printer_scope":
                (await admin.get(APIKey, key.key_id)).printer_ids = []
            else:
                (await admin.get(Group, key.group_id)).permissions = [p for p in full if p != "queue:read_all"]
            await admin.commit()
        await holder.commit()
    refreshed = await refreshing
    return {
        "refreshed": refreshed,
        "waited": waited,
        "facts": await _facts(farm.jobs),
        "audit": await _audits("archive", farm.archive_id),
    }


async def scenario_key_revoked_while_waiting() -> dict:
    """A refresh by an API key waits on a job row another writer holds; meanwhile the key loses the job's printer, or
    its owner loses queue read. Read again on the refresh's own session, the authority refuses: nothing changes."""
    _process_locks_off()
    return {revocation: await _key_revoked_while_waiting(revocation) for revocation in ("printer_scope", "queue_read")}


def _barrier(count: int = 2):
    """An awaitable that returns once ``count`` callers reached it."""
    reached, everyone = [0], asyncio.Event()

    async def wait():
        reached[0] += 1
        if reached[0] >= count:
            everyone.set()
        await asyncio.wait_for(everyone.wait(), 30)

    return wait


async def _declare_with(kind: str, source_id: int, seen: dict, user, value: bool, operation_id: str) -> str:
    from fastapi import HTTPException

    from backend.app.services import swap_source_declarations as decl

    try:
        result = await asyncio.wait_for(
            decl.declare(
                kind,
                source_id,
                value=value,
                expected_sha256=seen["sha256"],
                expected_revision=seen["revision"],
                operation_id=operation_id,
                actor=_actor(user),
                rights=_rights(),
            ),
            60,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        return f"{exc.status_code}:{detail.get('error', '')}"
    except Exception as exc:  # noqa: BLE001 — reported
        from backend.tests.integration.lock_barriers import sqlstate

        return "deadlock" if sqlstate(exc) == "40P01" else f"error:{type(exc).__name__}:{exc}"
    return "ok:replayed" if result["replayed"] else "ok"


async def _refresh_with(farm, user, listing, operation_id: str) -> str:
    from fastapi import HTTPException

    from backend.app.services import swap_job_refresh as jobs

    try:
        result = await asyncio.wait_for(
            jobs.refresh(
                "archive",
                farm.archive_id,
                declaration_id=listing["declaration_id"],
                source_revision=listing["source_revision"],
                jobs=[jobs.JobRef(entry["kind"], entry["id"], entry["revision"]) for entry in listing["jobs"]],
                operation_id=operation_id,
                actor=_actor(user),
                rights=_rights(),
                job_rights=_job_rights(),
            ),
            60,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        return f"{exc.status_code}:{detail.get('error', '')}"
    except Exception as exc:  # noqa: BLE001 — reported
        from backend.tests.integration.lock_barriers import sqlstate

        return "deadlock" if sqlstate(exc) == "40P01" else f"error:{type(exc).__name__}:{exc}"
    return "ok:replayed" if result["replayed"] else "ok"


async def _operation_lines(operation_id: str) -> int:
    from backend.app.core.database import async_session
    from backend.app.models.swap_source_declaration_audit import SwapSourceDeclarationAudit as Audit

    async with async_session() as db:
        return len((await db.execute(select(Audit.id).where(Audit.operation_id == operation_id))).all())


async def scenario_one_operation_sent_twice() -> dict:
    """The same request twice at once, from two sessions that both passed their preflight before either committed --
    a declaration and a refresh: one commit, one audit line, and both answer its result. Then the same operation id
    reused for ANOTHER source by two sessions nothing serializes: both pass every check, the UNIQUE index decides,
    and the loser is a typed conflict with nothing of it written -- never a replay of someone else's result."""
    from backend.app.services import swap_job_refresh as jobs, swap_source_declarations as decl

    _process_locks_off()
    _storage_guard_off()
    out: dict = {}
    user = await _user()

    # The same declaration twice: both copy the bytes before either writes.
    path = _data_dir() / "archive" / f"twice-{uuid.uuid4().hex[:6]}.gcode.3mf"
    _bytes(path, path.name)
    archive_id = await _archive(path, user)
    seen = await decl.inspect("archive", archive_id, actor=_actor(user), rights=_rights())
    real_read, both_read = decl._read_bytes, _barrier()

    async def read_then_meet(*args, **kwargs):
        result = await real_read(*args, **kwargs)
        await both_read()
        return result

    decl._read_bytes = read_then_meet
    operation = str(uuid.uuid4())
    try:
        outcomes = await asyncio.gather(
            *(_declare_with("archive", archive_id, seen, user, True, operation) for _ in range(2))
        )
    finally:
        decl._read_bytes = real_read
    out["declare"] = {"outcomes": sorted(outcomes), "lines": await _operation_lines(operation)}

    # The same refresh twice: both pass the preflight before either takes its locks.
    farm = await _archive_farm(user)
    listing = await jobs.candidates(
        "archive", farm.archive_id, actor=_actor(user), rights=_rights(), job_rights=_job_rights()
    )
    real_rights, both_checked = jobs._fresh_job_rights, _barrier()
    real_replayed, both_preflighted = jobs._replayed_visible, _barrier()
    calls = [0]

    async def preflight_then_meet(*args, **kwargs):
        result = await real_replayed(*args, **kwargs)
        calls[0] += 1
        if calls[0] <= 2:  # the two preflights; the locked re-reads pass straight through
            await both_preflighted()
        return result

    jobs._replayed_visible = preflight_then_meet
    operation = str(uuid.uuid4())
    try:
        outcomes = await asyncio.gather(*(_refresh_with(farm, user, listing, operation) for _ in range(2)))
    finally:
        jobs._replayed_visible = real_replayed
    out["refresh"] = {
        "outcomes": sorted(outcomes),
        "lines": await _operation_lines(operation),
        "facts": await _facts(farm.jobs),
    }

    # Different operators and archives leave both the account and archive guards independent.
    # Both pass their locked re-read of the operation and race to commit; the UNIQUE index decides.
    race_users = (user, await _user())
    sources = []
    for operator in race_users:
        path = _data_dir() / "archive" / f"race-{uuid.uuid4().hex[:6]}.gcode.3mf"
        _bytes(path, path.name)
        source_id = await _archive(path, operator)
        seen = await decl.inspect("archive", source_id, actor=_actor(operator), rights=_rights())
        sources.append((operator, source_id, seen))
    real_fresh, both_locked = decl._fresh_rights, _barrier()

    async def locked_then_meet(*args, **kwargs):
        result = await real_fresh(*args, **kwargs)
        await both_locked()
        return result

    decl._fresh_rights = locked_then_meet
    operation = str(uuid.uuid4())
    try:
        outcomes = await asyncio.gather(
            *(
                _declare_with("archive", source_id, seen, operator, True, operation)
                for operator, source_id, seen in sources
            )
        )
    finally:
        decl._fresh_rights = real_fresh
    out["declare_race"] = {
        "outcomes": sorted(outcomes),
        "lines": await _operation_lines(operation),
        "origins": sorted([str((await _source_state("archive", source_id))["origin"]) for _, source_id, _ in sources]),
    }

    farms = [await _archive_farm(operator) for operator in race_users]
    listings = [
        await jobs.candidates(
            "archive", f.archive_id, actor=_actor(operator), rights=_rights(), job_rights=_job_rights()
        )
        for f, operator in zip(farms, race_users, strict=True)
    ]

    async def rights_then_meet(*args, **kwargs):
        result = await real_rights(*args, **kwargs)
        await both_checked()
        return result

    jobs._fresh_job_rights = rights_then_meet
    operation = str(uuid.uuid4())
    try:
        outcomes = await asyncio.gather(
            *(
                _refresh_with(f, operator, listed, operation)
                for f, operator, listed in zip(farms, race_users, listings, strict=True)
            )
        )
    finally:
        jobs._fresh_job_rights = real_rights
    out["refresh_race"] = {
        "outcomes": sorted(outcomes),
        "lines": await _operation_lines(operation),
        "facts": sorted([await _facts(f.jobs) for f in farms], key=str),
    }
    return out


# --- 9. an old schema upgraded; job incarnations; an audit that cannot be written ------------------------------------


async def scenario_upgrade_and_identity() -> dict:
    """m203 over a schema from before it: the job incarnations filled once and unique, a new job and a clone born with
    their own, the sources without one; a declaration whose audit line cannot be written commits nothing."""
    from backend.app.core.database import async_session, engine, init_db
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services import swap_source_declarations as decl
    from backend.app.services.queue_ops import clone_item

    _process_locks_off()
    from backend.app.models.auto_queue import AutoQueueItem
    from backend.app.models.printer import Printer
    from backend.app.models.printer_queue import PrinterQueue

    async with async_session() as db:  # the jobs an install had before m203
        old = Printer(name="old", serial_number="OLDSCHEMA01", ip_address="192.0.2.61", access_code="00000000")
        db.add(old)
        await db.flush()
        db.add(PrinterQueue(id=old.id, printer_id=old.id, status="idle"))
        db.add_all([PrintQueueItem(queue_id=old.id, status="pending", position=n) for n in range(3)])
        db.add_all([AutoQueueItem(status="pending") for _ in range(2)])
        await db.commit()
        printer = old.id
    async with engine.begin() as conn:  # ...and the schema it had: none of m203's columns, indexes, table or record
        for table in ("print_queue", "auto_queue_items"):
            await conn.execute(text(f"DROP INDEX IF EXISTS ix_{table}_swap_declaration_identity"))
            await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS swap_declaration_identity"))
        for table in ("print_archives", "library_files"):
            await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS swap_source_declaration"))
        await conn.execute(text("DROP TABLE IF EXISTS swap_source_declaration_audit"))
        await conn.execute(text("DELETE FROM _migrations WHERE version = 203"))

    await init_db()  # the chain resumes: m203 runs over the old schema

    async with engine.connect() as conn:
        identities = [
            row[0]
            for table in ("print_queue", "auto_queue_items")
            for row in (await conn.execute(text(f"SELECT swap_declaration_identity FROM {table}"))).all()
        ]
        source_columns = (
            await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.columns WHERE column_name = 'swap_declaration_identity' "
                    "AND table_name IN ('print_archives', 'library_files')"
                )
            )
        ).all()
    async with async_session() as db:
        job = PrintQueueItem(queue_id=printer, status="pending", position=9)
        db.add(job)
        await db.commit()
        copy = await clone_item(db, job.id)
        await db.commit()
        born, cloned = job.swap_declaration_identity, copy.swap_declaration_identity

    user = await _user()
    path = _data_dir() / "archive" / "audit.gcode.3mf"
    _bytes(path, "audit")
    archive_id = await _archive(path, user)
    seen = await decl.inspect("archive", archive_id, actor=_actor(user), rights=_rights())
    real_audit = decl._audit_line

    def unwritable_audit(**fields):
        fields["actor_kind"] = None  # NOT NULL: the line cannot be written, so nothing of the declaration may stay
        return real_audit(**fields)

    decl._audit_line = unwritable_audit
    try:
        failed = await _declare("archive", archive_id, seen, user, True)
    finally:
        decl._audit_line = real_audit
    return {
        "identities": len(identities),
        "unique": len(set(identities)) == len(identities) and all(len(i or "") == 36 for i in identities),
        "source_identity_columns": [row[0] for row in source_columns],
        "born": len(born or ""),
        "clone_differs": bool(cloned) and cloned != born,
        "failed_declare": failed.split(":")[0],
        "after_failed": await _source_state("archive", archive_id),
        "audit_after_failed": await _audits("archive", archive_id),
    }


SCENARIOS = {
    "two_declares": scenario_two_declares,
    "scan_vs_declare": scenario_scan_vs_declare,
    "refresh_vs_assignment": scenario_refresh_vs_assignment,
    "refresh_vs_changes": scenario_refresh_vs_changes,
    "refresh_vs_source_delete": scenario_refresh_vs_source_delete,
    "upgrade_and_identity": scenario_upgrade_and_identity,
    "key_revoked_while_waiting": scenario_key_revoked_while_waiting,
    "one_operation_sent_twice": scenario_one_operation_sent_twice,
}
