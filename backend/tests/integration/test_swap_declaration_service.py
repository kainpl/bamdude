"""Inspect, then declare, over bytes verified twice (swap-source-declaration spec r3 §4, §5; plan T9.2).

Inspect copies the source a reprint would read, checks it is printable, discards the copy and answers its hash, its
current effective fact and an opaque revision. Declare copies it again: only the very bytes the operator saw, under
an unchanged revision and unchanged rights, get the operator's declaration -- with its audit line in the same
transaction. A replayed operation answers its first result; a changed one is refused. Busy, timeout, a missing file
and an unprintable one are the capture's own typed refusals, never retried.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.database import Base, configure_sqlite_connection
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.queue_source import QueueSource
from backend.app.models.swap_source_declaration_audit import SwapSourceDeclarationAudit
from backend.app.models.user import User
from backend.app.services import queue_sources, swap_compatibility as sc, swap_source_declarations as decl
from backend.tests.auth_helpers import mint_test_session
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

PLATE = {1: [{"id": 1, "type": "PLA", "color": "#FF0000", "used_g": "5"}]}


@pytest.fixture(autouse=True)
def clean_spool_state():
    queue_sources._reset_state()
    yield
    queue_sources._reset_state()


@pytest.fixture
async def factory(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'declare.db'}")
    event.listen(engine.sync_engine, "connect", configure_sqlite_connection)
    from backend.app.core.database import import_all_models

    import_all_models()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    made = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("backend.app.core.database.async_session", made)
    from backend.app.services import archive_write_scope, queue_ops

    # In-process locks of a previous test are bound to ITS event loop once anyone waited on them: fresh ones here.
    archive_write_scope._archive_locks.clear()
    queue_ops._queue_claim_locks.clear()
    yield made
    archive_write_scope._archive_locks.clear()
    queue_ops._queue_claim_locks.clear()
    await engine.dispose()


def a_3mf(path: Path, tag: str) -> bytes:
    import zipfile

    path.parent.mkdir(parents=True, exist_ok=True)
    write_routing_3mf(path, PLATE)
    with zipfile.ZipFile(path, "a") as zf:
        zf.writestr("Metadata/tag.txt", tag)
    return path.read_bytes()


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


async def a_user(factory, name="operator") -> User:
    async with factory() as db:
        user = User(username=name, email=f"{name}@example.com", password_hash="x", role="admin", is_active=True)
        db.add(user)
        await db.flush()
        await mint_test_session(db, user=user)
        return user


async def an_archive(factory, path: Path, *, owner: User | None = None, status="completed", record=None) -> int:
    raw = path.read_bytes()
    async with factory() as db:
        archive = PrintArchive(
            filename="plate.gcode.3mf",
            file_path=str(path),
            file_size=len(raw),
            status=status,
            source_content_hash=sha(raw),
            content_hash=sha(raw),
            created_by_id=owner.id if owner else None,
            extra_data={"swap_macro_events_pending": ["swap_mode_change_table"], "swap_hold": {"reason": "x"}},
        )
        if record is not None:
            sc.write_archive_fact(archive, record)
        db.add(archive)
        await db.commit()
        return archive.id


async def a_library_file(factory, path: Path, *, owner: User | None = None, file_hash="real") -> int:
    raw = path.read_bytes()
    async with factory() as db:
        row = LibraryFile(
            filename="plate.gcode.3mf",
            file_path=str(path),
            file_type="3mf",
            file_size=len(raw),
            file_hash=sha(raw) if file_hash == "real" else file_hash,
            is_external=True,
            swap_compatible=False,
            created_by_id=owner.id if owner else None,
        )
        db.add(row)
        await db.commit()
        return row.id


def actor_of(user: User) -> decl.Actor:
    return decl.Actor(kind="user", user_id=user.id, label=user.username, capture=user._auth_capture)


ALL = decl.Rights(read_all=True, update_all=True)


async def declare(kind, source_id, seen, user, *, value=True, operation_id=None):
    return await decl.declare(
        kind,
        source_id,
        value=value,
        expected_sha256=seen["sha256"],
        expected_revision=seen["revision"],
        operation_id=operation_id or str(uuid.uuid4()),
        actor=actor_of(user),
        rights=ALL,
    )


async def audits(factory) -> list[SwapSourceDeclarationAudit]:
    async with factory() as db:
        return list((await db.execute(select(SwapSourceDeclarationAudit))).scalars())


async def an_archive_row(factory, archive_id) -> PrintArchive:
    async with factory() as db:
        return await db.get(PrintArchive, archive_id)


# --- inspect ---------------------------------------------------------------------------------------------------------


async def test_inspect_answers_the_reprints_bytes_and_leaves_nothing_behind(factory, tmp_path):
    user = await a_user(factory)
    raw = a_3mf(tmp_path / "plate.gcode.3mf", "v1")
    archive_id = await an_archive(factory, tmp_path / "plate.gcode.3mf", owner=user)

    seen = await decl.inspect("archive", archive_id, actor=actor_of(user), rights=ALL)

    assert (seen["sha256"], seen["size_bytes"], seen["display_filename"]) == (sha(raw), len(raw), "plate.gcode.3mf")
    assert seen["current"] is None and seen["known"] is False  # a legacy archive: unknown
    assert len(seen["revision"]) == 64 and "path" not in seen and str(tmp_path) not in str(seen)
    async with factory() as db:
        assert (await db.execute(select(func.count()).select_from(QueueSource))).scalar_one() == 0
    assert not any(queue_sources.staging_root().glob("*.part")) if queue_sources.staging_root().exists() else True


async def test_inspect_refuses_what_cannot_be_printed(factory, tmp_path):
    user = await a_user(factory)
    broken = tmp_path / "broken.gcode.3mf"
    broken.write_bytes(b"not a zip")
    archive_id = await an_archive(factory, broken, owner=user)

    with pytest.raises(HTTPException) as refused:
        await decl.inspect("archive", archive_id, actor=actor_of(user), rights=ALL)
    assert refused.value.status_code in (400, 422)


async def test_a_raw_gcode_library_file_can_be_inspected(factory, tmp_path):
    user = await a_user(factory)
    path = tmp_path / "plate.gcode"
    path.write_text("; HEADER_BLOCK_START\nG28\nG1 X10\n")
    library_id = await a_library_file(factory, path, owner=user)

    seen = await decl.inspect("library", library_id, actor=actor_of(user), rights=ALL)

    assert seen["sha256"] == sha(path.read_bytes())


# --- declare ---------------------------------------------------------------------------------------------------------


async def test_the_bytes_the_operator_saw_get_the_declaration_and_one_audit_line(factory, tmp_path):
    user = await a_user(factory)
    raw = a_3mf(tmp_path / "plate.gcode.3mf", "v1")
    archive_id = await an_archive(factory, tmp_path / "plate.gcode.3mf", owner=user, record=False)
    seen = await decl.inspect("archive", archive_id, actor=actor_of(user), rights=ALL)

    done = await declare("archive", archive_id, seen, user, value=True)

    archive = await an_archive_row(factory, archive_id)
    assert sc.archive_source(archive) == (sc.SourceFact(True), sha(raw))
    assert sc.archive_fact(archive).value is False  # the executed run's fact is untouched
    assert archive.extra_data["swap_macro_events_pending"] == ["swap_mode_change_table"]  # nor its effects
    assert archive.extra_data["swap_hold"] == {"reason": "x"}
    (line,) = await audits(factory)
    assert (line.action, line.source_kind, line.source_id, line.sha256) == ("declare", "archive", archive_id, sha(raw))
    assert (line.old_value, line.new_value, line.declaration_id) == (False, True, done["declaration_id"])
    assert (line.actor_id, line.actor_label, line.actor_kind) == (user.id, "operator", "user")
    assert done["revision"] != seen["revision"]


async def test_bytes_that_changed_after_inspect_are_refused_and_nothing_is_written(factory, tmp_path):
    user = await a_user(factory)
    path = tmp_path / "plate.gcode.3mf"
    a_3mf(path, "v1")
    archive_id = await an_archive(factory, path, owner=user)
    seen = await decl.inspect("archive", archive_id, actor=actor_of(user), rights=ALL)
    a_3mf(path, "v2")  # same name, other bytes

    with pytest.raises(HTTPException) as refused:
        await declare("archive", archive_id, seen, user)

    assert refused.value.status_code == 409 and refused.value.detail["error"] == "swap_source_changed"
    assert (await an_archive_row(factory, archive_id)).swap_source_declaration is None
    assert await audits(factory) == []


@pytest.mark.parametrize("change", ["file_path", "owner", "status", "trashed", "epoch"])
async def test_a_source_that_moved_between_inspect_and_declare_is_refused(change, factory, tmp_path):
    from datetime import datetime, timezone

    user = await a_user(factory)
    other = await a_user(factory, "other")
    path = tmp_path / "plate.gcode.3mf"
    a_3mf(path, "v1")
    archive_id = await an_archive(factory, path, owner=user)
    seen = await decl.inspect("archive", archive_id, actor=actor_of(user), rights=ALL)
    async with factory() as db:
        archive = await db.get(PrintArchive, archive_id)
        if change == "file_path":
            copy = tmp_path / "copy.gcode.3mf"
            copy.write_bytes(path.read_bytes())
            archive.file_path = str(copy)
        elif change == "owner":
            archive.created_by_id = other.id
        elif change == "status":
            archive.status = "printing"
        elif change == "trashed":
            archive.deleted_at = datetime.now(timezone.utc)
        await db.commit()
    if change == "epoch":
        queue_sources.invalidate_receipts()  # a restore replaced the storage

    with pytest.raises(HTTPException) as refused:
        await declare("archive", archive_id, seen, user)

    assert refused.value.status_code in (404, 409)
    assert await audits(factory) == []


async def test_notes_and_energy_do_not_move_the_revision(factory, tmp_path):
    user = await a_user(factory)
    path = tmp_path / "plate.gcode.3mf"
    a_3mf(path, "v1")
    archive_id = await an_archive(factory, path, owner=user)
    seen = await decl.inspect("archive", archive_id, actor=actor_of(user), rights=ALL)
    async with factory() as db:
        archive = await db.get(PrintArchive, archive_id)
        archive.notes = "inspected the plate"
        archive.energy_kwh = 1.25
        archive.extra_data = {**archive.extra_data, "energy_is_approximate": True}
        await db.commit()

    done = await declare("archive", archive_id, seen, user)

    assert done["value"] is True


async def test_two_declarations_of_one_revision_commit_once(factory, tmp_path):
    user = await a_user(factory)
    path = tmp_path / "plate.gcode.3mf"
    a_3mf(path, "v1")
    archive_id = await an_archive(factory, path, owner=user)
    seen = await decl.inspect("archive", archive_id, actor=actor_of(user), rights=ALL)

    first = await declare("archive", archive_id, seen, user, value=True)
    with pytest.raises(HTTPException) as refused:
        await declare("archive", archive_id, seen, user, value=False)

    assert refused.value.status_code == 409 and refused.value.detail["error"] == "swap_source_changed"
    assert sc.archive_source(await an_archive_row(factory, archive_id))[0].value is True
    assert [line.declaration_id for line in await audits(factory)] == [first["declaration_id"]]


async def test_two_concurrent_declarations_of_one_revision_commit_once(factory, tmp_path, monkeypatch):
    user = await a_user(factory)
    path = tmp_path / "plate.gcode.3mf"
    a_3mf(path, "v1")
    library_id = await a_library_file(factory, path, owner=user)
    seen = await decl.inspect("library", library_id, actor=actor_of(user), rights=ALL)
    real_capture = decl._capture
    both = asyncio.Event()
    arrived = 0

    async def capture_then_meet(request):
        nonlocal arrived
        staged = await real_capture(request)
        arrived += 1
        if arrived == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 10)
        return staged

    monkeypatch.setattr(decl, "_capture", capture_then_meet)
    outcomes = await asyncio.gather(
        declare("library", library_id, seen, user, value=True),
        declare("library", library_id, seen, user, value=False),
        return_exceptions=True,
    )

    refused = [o for o in outcomes if isinstance(o, HTTPException)]
    done = [o for o in outcomes if isinstance(o, dict)]
    assert len(done) == 1 and len(refused) == 1 and refused[0].status_code == 409, outcomes
    assert len(await audits(factory)) == 1


async def test_an_audit_that_cannot_be_written_takes_the_declaration_with_it(factory, tmp_path, monkeypatch):
    user = await a_user(factory)
    path = tmp_path / "plate.gcode.3mf"
    a_3mf(path, "v1")
    archive_id = await an_archive(factory, path, owner=user)
    seen = await decl.inspect("archive", archive_id, actor=actor_of(user), rights=ALL)

    def broken_audit(**_fields):
        raise RuntimeError("audit refused")

    monkeypatch.setattr(decl, "_audit_line", broken_audit)
    with pytest.raises(RuntimeError):
        await declare("archive", archive_id, seen, user)

    assert (await an_archive_row(factory, archive_id)).swap_source_declaration is None
    assert await audits(factory) == []


async def test_a_replayed_operation_answers_once_and_a_changed_one_is_refused(factory, tmp_path):
    user = await a_user(factory)
    path = tmp_path / "plate.gcode.3mf"
    a_3mf(path, "v1")
    archive_id = await an_archive(factory, path, owner=user)
    seen = await decl.inspect("archive", archive_id, actor=actor_of(user), rights=ALL)
    operation = str(uuid.uuid4())

    first = await declare("archive", archive_id, seen, user, value=True, operation_id=operation)
    again = await declare("archive", archive_id, seen, user, value=True, operation_id=operation)
    with pytest.raises(HTTPException) as refused:
        await declare("archive", archive_id, seen, user, value=False, operation_id=operation)

    assert again["declaration_id"] == first["declaration_id"] and again["replayed"] is True
    assert refused.value.status_code == 409 and refused.value.detail["error"] == "swap_declaration_conflict"
    assert len(await audits(factory)) == 1


async def test_a_replay_by_someone_who_lost_the_source_reveals_nothing(factory, tmp_path):
    user = await a_user(factory)
    path = tmp_path / "plate.gcode.3mf"
    a_3mf(path, "v1")
    archive_id = await an_archive(factory, path, owner=user)
    seen = await decl.inspect("archive", archive_id, actor=actor_of(user), rights=ALL)
    operation = str(uuid.uuid4())
    await declare("archive", archive_id, seen, user, operation_id=operation)
    stranger = decl.Rights(read_all=False, update_all=False)
    stranger_user = await a_user(factory, "stranger")

    with pytest.raises(HTTPException) as refused:
        await decl.declare(
            "archive",
            archive_id,
            value=True,
            expected_sha256=seen["sha256"],
            expected_revision=seen["revision"],
            operation_id=operation,
            actor=actor_of(stranger_user),
            rights=stranger,
        )

    assert refused.value.status_code == 404
    assert seen["sha256"] not in str(refused.value.detail)


# --- the capture's own refusals ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "status"), [(queue_sources.QueueSourceBusy("busy"), 503), (queue_sources.CaptureTimeout("slow"), 504)]
)
async def test_a_busy_or_slow_capture_is_refused_once_and_never_retried(error, status, factory, tmp_path, monkeypatch):
    user = await a_user(factory)
    path = tmp_path / "plate.gcode.3mf"
    a_3mf(path, "v1")
    archive_id = await an_archive(factory, path, owner=user)
    calls = 0

    async def refusing(_request):
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(queue_sources, "capture", refusing)
    with pytest.raises(HTTPException) as refused:
        await decl.inspect("archive", archive_id, actor=actor_of(user), rights=ALL)

    assert refused.value.status_code == status and calls == 1


async def test_a_missing_file_is_a_typed_refusal_with_no_search_by_name(factory, tmp_path):
    user = await a_user(factory)
    path = tmp_path / "plate.gcode.3mf"
    a_3mf(path, "v1")
    archive_id = await an_archive(factory, path, owner=user)
    path.unlink()
    a_3mf(tmp_path / "other" / "plate.gcode.3mf", "v1")  # the same name elsewhere is never looked for

    with pytest.raises(HTTPException) as refused:
        await decl.inspect("archive", archive_id, actor=actor_of(user), rights=ALL)

    assert refused.value.status_code == 422


# --- library: no hash repair ------------------------------------------------------------------------------------------


async def test_a_library_file_without_a_hash_needs_a_refresh_first(factory, tmp_path, monkeypatch):
    user = await a_user(factory)
    path = tmp_path / "plate.gcode.3mf"
    a_3mf(path, "v1")
    library_id = await a_library_file(factory, path, owner=user, file_hash=None)

    async def never(_request):
        raise AssertionError("no capture before the refresh")

    monkeypatch.setattr(queue_sources, "capture", never)
    with pytest.raises(HTTPException) as refused:
        await decl.inspect("library", library_id, actor=actor_of(user), rights=ALL)

    assert refused.value.status_code == 409 and refused.value.detail["error"] == "library_refresh_required"


async def test_a_library_file_whose_bytes_differ_from_its_hash_is_refused_after_the_copy(factory, tmp_path):
    user = await a_user(factory)
    path = tmp_path / "plate.gcode.3mf"
    a_3mf(path, "v1")
    library_id = await a_library_file(factory, path, owner=user, file_hash="ab" * 32)

    with pytest.raises(HTTPException) as refused:
        await decl.inspect("library", library_id, actor=actor_of(user), rights=ALL)

    assert refused.value.status_code == 409 and refused.value.detail["error"] == "swap_source_changed"


async def test_a_library_declaration_writes_the_carrier_its_projection_and_the_tag(factory, tmp_path):
    user = await a_user(factory)
    path = tmp_path / "plate.gcode.3mf"
    raw = a_3mf(path, "v1")
    library_id = await a_library_file(factory, path, owner=user)
    seen = await decl.inspect("library", library_id, actor=actor_of(user), rights=ALL)

    await declare("library", library_id, seen, user, value=True)

    async with factory() as db:
        row = await db.get(LibraryFile, library_id)
        assert sc.library_fact(row) is True and row.swap_compatible is True
        assert row.file_hash == sha(raw)  # T9 never repairs the index
        assert "swap" in (row.file_tags or [])


async def test_a_printing_archive_cannot_be_declared(factory, tmp_path):
    user = await a_user(factory)
    path = tmp_path / "plate.gcode.3mf"
    a_3mf(path, "v1")
    archive_id = await an_archive(factory, path, owner=user, status="printing")

    with pytest.raises(HTTPException) as refused:
        await decl.inspect("archive", archive_id, actor=actor_of(user), rights=ALL)

    assert refused.value.status_code == 409 and refused.value.detail["error"] == "source_printing"
