"""A git backup carries an archive's swap declaration, and a restore grants it no authority -- spec r4 §11, plan T3.

The backup records the bool projection and the record itself, never the rest of ``extra_data``. A restore writes
the record through the one writer: a valid record keeps its value, a pending one comes back unknown and not
pending (a backup proves nothing about where a print came from now), a payload that is malformed or contradicts
its projection is unknown, and a backup taken before these fields existed says nothing -- unknown on insert, the
local record kept on overwrite. Only an explicit strict True in the backup brings a legacy True across.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.services import swap_compatibility as sc
from backend.app.services.git_backup import GitBackupService
from backend.app.services.git_restore import GitRestoreService, _CategoryTally

pytestmark = pytest.mark.integration


def a_row(name: str, **columns) -> PrintArchive:
    from datetime import datetime

    # A start time, so the restore matches the row by its natural key (filename + started_at) on overwrite.
    columns.setdefault("status", "completed")
    columns.setdefault("started_at", datetime(2026, 10, 1, 12, 0, 0))
    return PrintArchive(filename=f"{name}.gcode.3mf", file_path="", file_size=1, print_name=name, **columns)


async def backed_up(db, rows: list[PrintArchive]) -> list[dict]:
    db.add_all(rows)
    await db.commit()
    files: dict = {}
    await GitBackupService()._collect_archives(db, files)
    return files["archives/print_archives.json"]["archives"]


async def restore(db, entries, *, overwrite=False) -> _CategoryTally:
    tally = _CategoryTally()
    await GitRestoreService()._restore_archives(db, {"archives": entries}, overwrite, tally, {})
    await db.commit()
    return tally


async def by_name(db) -> dict[str, PrintArchive]:
    rows = (await db.execute(select(PrintArchive).execution_options(populate_existing=True))).scalars().all()
    return {row.print_name: row for row in rows}


async def test_git_roundtrip_preserves_true_false_unknown(db_session):
    rows = {name: a_row(name) for name in ("true", "false", "unknown", "pending", "legacy-true", "legacy-false")}
    sc.write_archive_fact(rows["true"], True)
    sc.write_archive_fact(rows["false"], False)
    sc.write_archive_fact(rows["unknown"], None)
    sc.write_archive_fact(rows["pending"], None, pending_external=True)
    rows["legacy-true"].swap_compatible = True
    rows["legacy-false"].swap_compatible = False
    rows["true"].extra_data = {**rows["true"].extra_data, "_print_data": {"gcode_file": "x"}, "plate_id": 3}
    entries = await backed_up(db_session, list(rows.values()))

    # Only the declaration travels, never the runtime metadata around it.
    entry = next(e for e in entries if e["print_name"] == "true")
    assert "_print_data" not in str(entry) and "extra_data" not in entry
    for row in (await db_session.execute(select(PrintArchive))).scalars():
        await db_session.delete(row)
    await db_session.commit()

    await restore(db_session, entries)

    restored = await by_name(db_session)
    assert {name: sc.archive_fact(row).value for name, row in restored.items()} == {
        "true": True,
        "false": False,
        "unknown": None,
        "pending": None,
        "legacy-true": True,
        "legacy-false": None,
    }
    assert not any(sc.archive_fact(row).pending_external for row in restored.values())


async def test_old_git_backup_absence_differs_from_explicit_unknown(db_session):
    local = a_row("local")
    sc.write_archive_fact(local, False)
    db_session.add(local)
    await db_session.commit()
    entries = await backed_up(db_session, [])
    (entry,) = entries
    old_format = {key: value for key, value in entry.items() if key not in (sc.CARRIER_KEY, "swap_compatible")}

    # A backup from before these fields: it says nothing, so an overwrite keeps the local record...
    await restore(db_session, [old_format], overwrite=True)
    assert sc.archive_fact((await by_name(db_session))["local"]) == sc.ArchiveFact(False, recorded=True)

    # ...while an explicit null in the backup is an answer: unknown.
    await restore(db_session, [{**old_format, sc.CARRIER_KEY: None}], overwrite=True)
    assert sc.archive_fact((await by_name(db_session))["local"]) == sc.ArchiveFact(None, recorded=True)

    # And inserted from the old format, the row is unknown -- not a False nobody declared.
    for row in (await db_session.execute(select(PrintArchive))).scalars():
        await db_session.delete(row)
    await db_session.commit()
    await restore(db_session, [old_format])
    assert sc.archive_fact((await by_name(db_session))["local"]).value is None


@pytest.mark.parametrize(
    ("projection", "record"),
    [
        (True, {"version": 1, "value": "yes", "pending_external": False}),
        (False, {"version": 1, "value": True, "pending_external": False}),  # contradicts its projection
        (True, {"version": 2, "value": True, "pending_external": False}),
        (True, "forged"),
        ("true", None),
    ],
    ids=["bad value", "conflict", "other version", "not a dict", "projection not a bool"],
)
async def test_an_invalid_backed_up_record_restores_as_unknown(projection, record, db_session):
    entry = {"id": 9, "filename": "x.3mf", "print_name": "x", "status": "completed", "swap_compatible": projection}
    entry["started_at"] = "2026-10-01T12:00:00"
    entry[sc.CARRIER_KEY] = record

    await restore(db_session, [entry])

    row = (await by_name(db_session))["x"]
    assert sc.archive_fact(row) == sc.ArchiveFact(None, recorded=True)


async def test_a_legacy_true_comes_across_only_as_an_explicit_strict_bool(db_session):
    # Separate physical starts provide identities independently of declarations.
    base = {"filename": "x.3mf", "status": "completed"}
    await restore(
        db_session,
        [
            {**base, "id": 1, "print_name": "strict", "swap_compatible": True, "started_at": "2026-10-01T12:00:00"},
            {**base, "id": 2, "print_name": "truthy", "swap_compatible": 1, "started_at": "2026-10-01T13:00:00"},
            {**base, "id": 3, "print_name": "absent", "started_at": "2026-10-01T14:00:00"},
        ],
    )
    restored = await by_name(db_session)
    assert sc.archive_fact(restored["strict"]).value is True
    assert sc.archive_fact(restored["truthy"]).value is None
    assert sc.archive_fact(restored["absent"]).value is None


async def test_restored_pending_has_no_external_or_dispatch_authority(db_session, tmp_path, printer_factory):
    from backend.app.core.config import settings
    from backend.app.services.archive import ArchiveService
    from backend.tests.integration.test_dispatch_without_original import a_sliced_file

    printer = await printer_factory()
    pending = a_row("pending", status="printing", printer_id=printer.id)
    sc.write_archive_fact(pending, None, pending_external=True)
    entries = await backed_up(db_session, [pending])
    for row in (await db_session.execute(select(PrintArchive))).scalars():
        await db_session.delete(row)
    await db_session.commit()

    await restore(db_session, entries)
    restored = (await by_name(db_session))["pending"]
    assert restored.status == "cancelled" and restored.file_path == ""
    assert restored.extra_data["recovered_outcome_uncertain"] is True
    assert sc.archive_fact(restored) == sc.ArchiveFact(None, recorded=True)

    # A manual retry that downloads a marked file now does not promote it: the backup proved no origin.
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "dl").mkdir()
    downloaded = a_sliced_file(tmp_path / "dl" / "pending.swap.gcode.3mf")
    await ArchiveService(db_session).attach_3mf_to_archive(
        restored.id, downloaded, downloaded.name, received_name=downloaded.name
    )
    # The attach renames the row after the file's own metadata; read it back by id.
    restored = await db_session.get(PrintArchive, restored.id, populate_existing=True)
    assert sc.archive_fact(restored) == sc.ArchiveFact(None, recorded=True)


async def test_pending_without_a_proven_printer_is_skipped_without_authority(db_session):
    pending = a_row("unproven-pending", status="printing")
    sc.write_archive_fact(pending, None, pending_external=True)
    entries = await backed_up(db_session, [pending])
    await db_session.delete(pending)
    await db_session.commit()

    tally = await restore(db_session, entries)

    assert await by_name(db_session) == {}
    assert tally.restored == 0 and tally.skipped == 1 and tally.failed == 0


async def test_the_backup_carries_the_source_hash_for_identity(db_session):
    row = a_row("hashed", source_content_hash="a" * 64)
    sc.write_archive_fact(row, False)
    (entry,) = await backed_up(db_session, [row])
    assert entry["source_content_hash"] == "a" * 64
    for existing in (await db_session.execute(select(PrintArchive))).scalars():
        await db_session.delete(existing)
    await db_session.commit()

    await restore(db_session, [entry])

    assert (await by_name(db_session))["hashed"].source_content_hash == "a" * 64
