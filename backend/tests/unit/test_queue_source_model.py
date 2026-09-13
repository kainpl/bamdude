"""The ``queue_sources`` table, the two per-job columns and the one descriptor.

Spec: ``60-specs/queue-source-spool-spec.md`` §4 (files and data model).

What the storage layer has to refuse is the point of this file. A queue source
is the bytes a job prints, so the three things that would silently break the
guarantee — two rows claiming the same hash, a negative size, a state nothing
downstream knows how to read — are refused by the database itself rather than by
whichever service happened to write them. On SQLite a CHECK *is* enforced
(unlike a foreign key, which this codebase never enables), so these hold on both
backends.

The second half pins the descriptor: spec §7 asks for ONE internal shape every
reader takes a job's source from, and the field set is the contract Task 6
onward consumes. A dataclass with no I/O — building one must never touch a disk
or a session.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.queue_source import (
    QUEUE_SOURCE_FORMATS,
    QUEUE_SOURCE_STATES,
    STATE_BROKEN,
    STATE_DELETING,
    STATE_READY,
    QueueSource,
)
from backend.app.services.queue_source_descriptor import (
    SOURCE_SNAPSHOT_VERSION,
    SOURCE_STORAGE_STATES,
    QueueSourceDescriptor,
    source_snapshot,
    source_storage_state,
)

pytestmark = pytest.mark.unit


def _source(**kwargs) -> QueueSource:
    defaults = {
        "sha256": "a" * 64,
        "size_bytes": 1234,
        "relative_path": "queue-spool/objects/aa/" + "a" * 64 + ".3mf",
        "format": "3mf",
        "state": STATE_READY,
    }
    defaults.update(kwargs)
    return QueueSource(**defaults)


# ── The table ───────────────────────────────────────────────────────────────


async def test_a_ready_source_round_trips(db_session):
    db_session.add(_source())
    await db_session.commit()

    row = (await db_session.execute(select(QueueSource))).scalar_one()
    assert row.sha256 == "a" * 64
    assert row.size_bytes == 1234
    assert row.format == "3mf"
    assert row.state == STATE_READY
    assert row.created_at is not None
    # Only the GC's hint, and only once the last owner let go (spec §9).
    assert row.unreferenced_at is None


async def test_the_same_bytes_are_one_row(db_session):
    """S4/A07: the hash is the dedup key, so a second row for it is a bug.

    A UNIQUE conflict answered as a 500 is exactly what A07 forbids, which is
    why the writer has to be able to see this refusal rather than race past it.
    """
    db_session.add(_source())
    await db_session.commit()

    db_session.add(_source(relative_path="queue-spool/objects/aa/other.3mf"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.parametrize("value", ["", "a" * 63, "a" * 65, "short"])
async def test_only_a_full_length_sha256_is_accepted(db_session, value):
    """Spec §4 says the hash is non-empty, and NOT NULL alone admits ``''`` and a
    truncated value. A hex SHA-256 is 64 characters forever, so the length is a
    fact the column can hold — and it has to be held HERE: SQLite cannot add a
    CHECK to an existing table without rebuilding it, so a constraint written
    after m173 ships would never reach an upgraded database."""
    db_session.add(_source(sha256=value))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_a_negative_size_is_refused(db_session):
    db_session.add(_source(size_bytes=-1))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_an_empty_file_is_allowed_by_the_size_check(db_session):
    """``>= 0``, not ``> 0``: refusing a zero-byte capture is the *writer's*
    job (a truncated read is not a valid source), and the column may not
    pre-empt a decision that needs the format and the CRC to make."""
    db_session.add(_source(size_bytes=0))
    await db_session.commit()
    assert (await db_session.execute(select(QueueSource.size_bytes))).scalar_one() == 0


@pytest.mark.parametrize("state", QUEUE_SOURCE_STATES)
async def test_each_of_the_three_states_is_accepted(db_session, state):
    db_session.add(_source(state=state))
    await db_session.commit()
    assert (await db_session.execute(select(QueueSource.state))).scalar_one() == state


async def test_the_state_vocabulary_is_closed(db_session):
    """``preparing`` is an API word (spec §8), never a stored blob state: a
    staging file is not a QueueSource row at all (spec §4)."""
    assert QUEUE_SOURCE_STATES == (STATE_READY, STATE_DELETING, STATE_BROKEN)

    db_session.add(_source(state="preparing"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.parametrize("fmt", QUEUE_SOURCE_FORMATS)
async def test_both_formats_are_accepted(db_session, fmt):
    db_session.add(_source(format=fmt))
    await db_session.commit()
    assert (await db_session.execute(select(QueueSource.format))).scalar_one() == fmt


async def test_an_unknown_format_is_refused(db_session):
    """The extension on disk follows the verified format, and the raw-gcode
    exemption downstream branches on it — a third value would reach code that
    has no branch for it (spec §4)."""
    db_session.add(_source(format="stl"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


# ── The two per-job columns ─────────────────────────────────────────────────


async def test_a_queue_item_may_name_no_source_and_then_one(db_session, printer_factory):
    """NULL is the legacy row every existing install is full of (spec §8); the
    id is what a captured one carries. Both queues, same shape."""
    printer = await printer_factory()
    queue = PrinterQueue(id=printer.id, printer_id=printer.id)
    db_session.add(queue)
    source = _source()
    db_session.add(source)
    await db_session.commit()

    legacy = PrintQueueItem(queue_id=queue.id, library_file_id=None, position=1, created_by_id=None)
    snapshotted = PrintQueueItem(
        queue_id=queue.id,
        position=2,
        created_by_id=None,
        queue_source_id=source.id,
        source_snapshot={"version": SOURCE_SNAPSHOT_VERSION, "display_filename": "lamp.gcode.3mf"},
    )
    auto_legacy = AutoQueueItem(position=1)
    auto_snapshotted = AutoQueueItem(
        position=2,
        queue_source_id=source.id,
        source_snapshot={"version": SOURCE_SNAPSHOT_VERSION, "display_filename": "lamp.gcode.3mf"},
    )
    db_session.add_all([legacy, snapshotted, auto_legacy, auto_snapshotted])
    await db_session.commit()

    assert legacy.queue_source_id is None
    assert legacy.source_snapshot is None
    assert snapshotted.queue_source_id == source.id
    assert snapshotted.source_snapshot["display_filename"] == "lamp.gcode.3mf"
    assert auto_legacy.queue_source_id is None
    assert auto_snapshotted.queue_source_id == source.id
    assert auto_snapshotted.source_snapshot["version"] == SOURCE_SNAPSHOT_VERSION


async def test_the_snapshot_column_keeps_a_dict_as_a_dict(db_session, printer_factory):
    """Versioned metadata, not a hand-serialised string: every reader from
    Task 6 on takes ``provenance`` and the display fields out of it."""
    printer = await printer_factory()
    queue = PrinterQueue(id=printer.id, printer_id=printer.id)
    db_session.add(queue)
    await db_session.commit()

    item = PrintQueueItem(
        queue_id=queue.id,
        position=1,
        created_by_id=None,
        source_snapshot={
            "version": SOURCE_SNAPSHOT_VERSION,
            "provenance": {"kind": "library_file", "id": 9},
            "plate_fallback": 2,
        },
    )
    db_session.add(item)
    await db_session.commit()
    item_id = item.id
    db_session.expire_all()

    reloaded = (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()
    assert reloaded.source_snapshot["provenance"] == {"kind": "library_file", "id": 9}
    assert reloaded.source_snapshot["plate_fallback"] == 2


async def test_the_source_column_is_indexed_on_both_queues(db_session):
    """Every GC pass and every "who still owns this blob" query is a lookup by
    it across both tables (spec §9)."""
    for table in ("print_queue", "auto_queue_items"):
        rows = (await db_session.execute(text(f"PRAGMA index_list({table})"))).fetchall()
        assert f"ix_{table}_queue_source_id" in {row[1] for row in rows}


@pytest.mark.parametrize("model", [PrintQueueItem, AutoQueueItem])
def test_deleting_an_archive_no_longer_deletes_the_job(model):
    """Spec §4/§10: the one cascade that destroyed a job because its source went
    away. A snapshot-backed job outlives its archive, so the navigational FK
    nulls instead — the code-level detach SQLite needs lands in Task 9."""
    (fk,) = [fk for fk in model.__table__.foreign_key_constraints if [c.name for c in fk.columns] == ["archive_id"]]
    assert fk.ondelete == "SET NULL"


@pytest.mark.parametrize("model", [PrintQueueItem, AutoQueueItem])
def test_the_source_reference_refuses_to_let_the_blob_go(model):
    """RESTRICT, not CASCADE and not SET NULL: a row with a live owner may not
    be deleted out from under it, and losing the reference silently would turn
    a runnable job into one with no source at all (spec §4, S5)."""
    (fk,) = [
        fk for fk in model.__table__.foreign_key_constraints if [c.name for c in fk.columns] == ["queue_source_id"]
    ]
    assert fk.referred_table.name == "queue_sources"
    assert fk.ondelete == "RESTRICT"


# ── The descriptor ──────────────────────────────────────────────────────────


def test_the_descriptor_carries_exactly_the_agreed_fields():
    fields = [f.name for f in dataclasses.fields(QueueSourceDescriptor)]
    assert fields == [
        "path",
        "format",
        "sha256",
        "size_bytes",
        "display_filename",
        "plate_fallback",
        "provenance",
    ]


def test_the_descriptor_is_frozen():
    """S3: after capture the file is immutable, and so is what names it — a
    reader that could re-point ``path`` would be a second source of truth."""
    descriptor = QueueSourceDescriptor(
        path=Path("queue-spool/objects/aa/aa.3mf"),
        format="3mf",
        sha256="a" * 64,
        size_bytes=10,
        display_filename="lamp.gcode.3mf",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        descriptor.path = Path("elsewhere")  # type: ignore[misc]


def test_the_descriptor_defaults_are_the_unknown_ones():
    descriptor = QueueSourceDescriptor(
        path=Path("x.3mf"),
        format="3mf",
        sha256="b" * 64,
        size_bytes=10,
        display_filename="x.3mf",
    )
    assert descriptor.plate_fallback is None
    assert descriptor.provenance == {}
    # default_factory, not a shared dict
    other = QueueSourceDescriptor(
        path=Path("y.3mf"), format="3mf", sha256="c" * 64, size_bytes=10, display_filename="y.3mf"
    )
    assert descriptor.provenance is not other.provenance


def test_the_display_name_is_not_the_hash():
    """Spec §4: the human name travels beside the bytes; the hash may never
    replace it in the UI or on the printer."""
    descriptor = QueueSourceDescriptor(
        path=Path("queue-spool/objects/aa/" + "a" * 64 + ".3mf"),
        format="3mf",
        sha256="a" * 64,
        size_bytes=10,
        display_filename="Настільна лампа.gcode.3mf",
    )
    assert descriptor.display_filename == "Настільна лампа.gcode.3mf"
    assert descriptor.sha256 not in descriptor.display_filename


# ── The stored snapshot payload ─────────────────────────────────────────────


def test_the_snapshot_version_is_one_named_number():
    """Nothing else may spell this. ``filament_policy.VERSION`` is the precedent
    and also the warning: its decoder compares the version for exact equality, so
    a writer that invented a second spelling would silently degrade every row it
    touched."""
    assert SOURCE_SNAPSHOT_VERSION == 1


def test_the_snapshot_is_built_from_the_descriptor_and_stamped():
    """One builder, one layout (spec §4: provenance, display filename, format,
    plate fallback). A capture that hand-wrote the dict would be free to drop a
    key every later reader expects."""
    descriptor = QueueSourceDescriptor(
        path=Path("queue-spool/objects/aa/" + "a" * 64 + ".3mf"),
        format="3mf",
        sha256="a" * 64,
        size_bytes=4096,
        display_filename="lamp.gcode.3mf",
        plate_fallback=2,
        provenance={"kind": "library_file", "id": 9},
    )

    snapshot = source_snapshot(descriptor)

    assert snapshot == {
        "version": SOURCE_SNAPSHOT_VERSION,
        "provenance": {"kind": "library_file", "id": 9},
        "display_filename": "lamp.gcode.3mf",
        "format": "3mf",
        "plate_fallback": 2,
    }
    # No hash and no path: the snapshot is the job's metadata, and the bytes are
    # identified by ``queue_source_id`` alone. A path copied in here would be a
    # second, rottable truth about where the file is.
    assert "sha256" not in snapshot
    assert "path" not in snapshot and "relative_path" not in snapshot


def test_the_snapshot_does_not_share_the_descriptors_provenance_dict():
    """The row's JSON is mutable and the descriptor is frozen — handing out the
    same dict would let a writer reach back into a value object."""
    descriptor = QueueSourceDescriptor(
        path=Path("x.3mf"),
        format="3mf",
        sha256="b" * 64,
        size_bytes=1,
        display_filename="x.3mf",
        provenance={"kind": "archive", "id": 3},
    )

    snapshot = source_snapshot(descriptor)
    snapshot["provenance"]["id"] = 99

    assert descriptor.provenance == {"kind": "archive", "id": 3}


# ── The API-facing storage state ────────────────────────────────────────────


def test_the_five_api_states_are_the_spec_ones():
    assert SOURCE_STORAGE_STATES == ("ready", "preparing", "legacy", "broken", "exempt")


def test_a_row_with_no_source_is_legacy():
    assert source_storage_state(queue_source_id=None) == "legacy"


def test_an_external_print_is_exempt():
    """Spec §2: a print BamDude never sent has no supported source at the
    moment its row is made — that is not a violation of S1, and calling it
    ``legacy`` would promise a hydration that must never happen."""
    assert source_storage_state(queue_source_id=None, origin="external") == "exempt"
    assert source_storage_state(queue_source_id=None, is_calibration=True) == "exempt"
    # A direct print is NOT exempt: BamDude sends it from a local sliced source.
    assert source_storage_state(queue_source_id=None, origin="direct") == "legacy"


def test_only_an_attached_ready_blob_is_ready():
    """Spec §8: ``ready`` is set for an attached ready blob, never inferred
    from the kind of the original source."""
    assert source_storage_state(queue_source_id=7, blob_state=STATE_READY) == "ready"
    assert source_storage_state(queue_source_id=7, blob_state=STATE_BROKEN) == "broken"
    assert source_storage_state(queue_source_id=7, blob_state=STATE_DELETING) == "broken"
    # The blob was not loaded — the caller does not know, so it may not claim.
    assert source_storage_state(queue_source_id=7, blob_state=None) == "legacy"


def test_preparing_comes_from_the_live_supervisor_and_outranks_a_missing_blob():
    """Spec §8: ``preparing`` may be derived from a live supervisor, and after a
    crash the same row reads ``legacy`` again — so it is an argument, never a
    stored column."""
    assert source_storage_state(queue_source_id=None, preparing=True) == "preparing"
    # Exempt still wins: there is nothing to prepare.
    assert source_storage_state(queue_source_id=None, preparing=True, origin="external") == "exempt"
