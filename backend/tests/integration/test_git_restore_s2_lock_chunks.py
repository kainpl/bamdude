"""Restore planning and committed-proof reads keep a bounded global lock order."""

import pytest
from sqlalchemy import event

from backend.app.models.archive import PrintArchive
from backend.app.services import product_facets
from backend.app.services.git_restore import _parse_dt
from backend.app.services.git_restore_identity import parse_archive, prepare, usage_basis
from backend.app.services.git_restore_records import ArchiveProof
from backend.tests.integration.test_git_restore_s2 import AT, entry, restore

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("phase", ["prepare", "usage_basis"])
async def test_archive_locks_are_complete_sorted_and_bounded(db_session, monkeypatch, phase):
    rows = [
        PrintArchive(filename=f"run-{i}", file_path="", file_size=0, started_at=AT, status="completed")
        for i in range(11)
    ]
    db_session.add_all(rows)
    await db_session.commit()
    expected_ids = sorted(row.id for row in rows)
    monkeypatch.setattr(product_facets, "SQL_CHUNK", 3)
    calls, commits = [], []
    engine = db_session.get_bind()

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        if "print_archives.id IN (" in statement:
            calls.append(tuple(parameters))

    def commit(_conn):
        commits.append(True)

    event.listen(engine, "before_cursor_execute", capture)
    event.listen(engine, "commit", commit)
    try:
        if phase == "prepare":
            records = [parse_archive(entry(recovery_identity=r.recovery_identity), _parse_dt) for r in reversed(rows)]
            # Repeated candidate input must not lock the same id twice.
            planned, _, _ = await prepare(db_session, records + records[:2])
            assert planned == set(expected_ids)
        else:
            mapping = {i: r.id for i, r in enumerate(reversed(rows), 1)}
            proofs = {i: ArchiveProof(r.id, r.recovery_identity, None) for i, r in enumerate(reversed(rows), 1)}
            _, valid = await usage_basis(db_session, [], mapping, proofs)
            assert valid == proofs
        assert [len(chunk) for chunk in calls] == [3, 3, 3, 2]
        assert [key for chunk in calls for key in chunk] == expected_ids
        assert commits == []  # Every chunk is still owned by this transaction.
    finally:
        event.remove(engine, "before_cursor_execute", capture)
        event.remove(engine, "commit", commit)


async def test_same_backup_uuid_neighbours_remain_conservatively_conflicting(db_session):
    from uuid import uuid4

    from sqlalchemy import select

    first, second = str(uuid4()), str(uuid4())
    rows = [
        entry(
            id=1,
            recovery_identity=first,
            printer_id=100,
            printer_serial="UNPROVEN-A",
            started_at="2026-10-01T12:00:00.123456",
            notes="first",
        ),
        entry(
            id=2,
            recovery_identity=second,
            printer_id=101,
            printer_serial="UNPROVEN-B",
            started_at="2026-10-01T12:00:00.123456",
            notes="second",
        ),
    ]
    outcome = (await restore(db_session, rows, overwrite=False))["archives"]
    assert (outcome.restored, outcome.skipped, outcome.failed) == (1, 1, 0)
    current = (await db_session.scalars(select(PrintArchive))).one()
    assert current.recovery_identity == first and current.notes == "first" and current.printer_id is None
    repeated = (await restore(db_session, rows, overwrite=False))["archives"]
    assert (repeated.restored, repeated.skipped, repeated.failed) == (0, 2, 0)
    assert next(n["params"] for n in repeated.notes if n["code"] == "archivesInputSummary")["identity_skipped"] == 1
    assert len((await db_session.scalars(select(PrintArchive))).all()) == 1
