"""The one source-link rule names a proven incarnation or nothing (G03 B, A27; TA37 (i) and the rule's cases)."""

import json
from types import SimpleNamespace

import pytest

from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.services import queue_source_link as link
from backend.app.services.queue_source_capture import plan_capture

A = "11111111-1111-1111-1111-111111111111"
B = "22222222-2222-2222-2222-222222222222"


def job(*, owns=True, provenance=None, intent=None, archive_id=None, library_file_id=None):
    return SimpleNamespace(
        queue_source_id=5 if owns else None,
        source_snapshot={"version": 1, "provenance": provenance} if provenance is not None else None,
        filament_routing=json.dumps({"version": 2, "source_identity": intent}) if intent is not None else None,
        archive_id=archive_id,
        library_file_id=library_file_id,
    )


def parent(identity):
    return SimpleNamespace(recovery_identity=identity)


def test_the_stamp_is_the_row_s_own_identity():
    assert link.stamp({"kind": "archive", "id": 3}, parent(A)) == {"kind": "archive", "id": 3, "incarnation": A}
    assert link.stamp({"kind": "archive", "id": 3}, parent(None)) == {"kind": "archive", "id": 3}


def test_a_captured_job_s_candidate_is_its_provenance():
    candidate = link.candidate_of(job(provenance={"kind": "archive", "id": 3, "incarnation": A}, archive_id=9))
    assert candidate == link.Candidate("archive", 3, A, True)
    assert link.candidate_of(job(provenance={"kind": "library_file", "id": 4})) == link.Candidate(
        "library_file", 4, None, True
    )
    assert link.candidate_of(job(provenance={"kind": "elsewhere", "id": 4})) is None


def test_a_legacy_job_s_candidate_is_its_intent_then_its_live_link():
    assert link.candidate_of(job(owns=False, intent={"kind": "library", "id": 4})) == link.Candidate(
        "library_file", 4, None, False
    )
    assert link.candidate_of(job(owns=False, archive_id=8)) == link.Candidate("archive", 8, None, False)
    assert link.candidate_of(job(owns=False, archive_id=8), live_fallback=False) is None


@pytest.mark.parametrize(
    ("candidate", "row", "expected"),
    [
        (link.Candidate("archive", 3, A, True), parent(A), (3, None)),  # the same row
        (link.Candidate("archive", 3, A, True), parent(B), (None, None)),  # another row under the id
        (link.Candidate("archive", 3, A, True), None, (None, None)),  # gone
        (link.Candidate("archive", 3, None, True), parent(A), (None, None)),  # captured before the stamp
        (link.Candidate("library_file", 4, A, True), parent(A), (None, 4)),
        (link.Candidate("library_file", 4, None, False), parent(B), (None, 4)),  # legacy: present -> restored
        (link.Candidate("library_file", 4, None, False), None, (None, None)),  # legacy: missing -> NULL
        (None, parent(A), (None, None)),
    ],
)
def test_the_write_names_a_proven_incarnation_or_nothing(candidate, row, expected):
    assert link.link_for(candidate, row) == expected


def test_never_the_job_s_own_execution_archive():
    candidate = link.Candidate("archive", 9, A, True)
    assert link.link_for(candidate, parent(A), execution_archive_id=9) == (None, None)


def test_an_unchanged_live_link_is_recognised():
    candidate = link.Candidate("archive", 3, A, True)
    assert link.live_link_names(job(archive_id=3), candidate)
    assert not link.live_link_names(job(archive_id=9), candidate)
    assert not link.live_link_names(job(archive_id=3, library_file_id=4), candidate)


def test_the_capture_stamps_its_provenance(tmp_path):
    """TA37 (i): ``plan_capture`` writes the incarnation of the row it captures from."""
    path = tmp_path / "lamp.3mf"
    path.write_bytes(b"x")
    row = LibraryFile(id=4, filename="lamp.3mf", file_path=str(path), file_type="3mf", file_size=1, recovery_identity=A)
    request = plan_capture(library_file=row)
    assert request.provenance == {"kind": "library_file", "id": 4, "incarnation": A}


@pytest.mark.asyncio
async def test_the_parent_is_read_again_under_its_lock(db_session):
    archive = PrintArchive(filename="a.3mf", file_path="", file_size=0, status="completed")
    db_session.add(archive)
    await db_session.commit()
    candidate = link.Candidate("archive", archive.id, archive.recovery_identity, True)
    row = await link.lock_parent(db_session, candidate)
    assert row is not None and link.link_for(candidate, row) == (archive.id, None)
    assert await link.lock_parent(db_session, link.Candidate("archive", archive.id + 100, A, True)) is None
