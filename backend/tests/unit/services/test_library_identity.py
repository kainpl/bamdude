"""``LibraryIdentity`` is the one proof of a library row and compares the row's own identity (G03 B, A32; TA42)."""

from datetime import datetime
from types import SimpleNamespace

from backend.app.services import library_identity
from backend.app.services.library_identity import LibraryIdentity

CREATED = datetime(2026, 11, 20, 12, 0, 0)


def _row(identity="11111111-1111-1111-1111-111111111111", **changes):
    values = {
        "id": 7,
        "file_path": "library/jar.3mf",
        "file_hash": "a" * 64,
        "created_at": CREATED,
        "recovery_identity": identity,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_capture_carries_the_rows_identity():
    assert LibraryIdentity.capture(_row()).recovery_identity == "11111111-1111-1111-1111-111111111111"


def test_the_same_row_matches():
    proof = LibraryIdentity.capture(_row())
    assert proof.matches(_row())


def test_a_row_that_took_this_ones_id_path_hash_and_time_is_another_row():
    """SQLite: the row deleted, the share's file registered again in the same second."""
    proof = LibraryIdentity.capture(_row())
    assert not proof.matches(_row(identity="22222222-2222-2222-2222-222222222222"))


def test_every_other_field_still_counts():
    proof = LibraryIdentity.capture(_row())
    assert not proof.matches(_row(file_path="library/other.3mf"))
    assert not proof.matches(_row(file_hash="b" * 64))
    assert not proof.matches(_row(created_at=datetime(2026, 11, 20, 12, 0, 1)))
    assert not proof.matches(_row(id=8))
    assert not proof.matches(None)


def test_a_capture_without_an_identity_is_compared_by_the_other_fields():
    legacy = LibraryIdentity.capture(
        SimpleNamespace(id=7, file_path="library/jar.3mf", file_hash="a" * 64, created_at=CREATED)
    )
    assert legacy.recovery_identity is None
    assert legacy.matches(_row())
    assert not legacy.matches(_row(file_hash="b" * 64))


def test_a_row_without_an_identity_never_matches_a_proof_that_has_one():
    assert not LibraryIdentity.capture(_row()).matches(_row(identity=None))


def test_there_is_no_way_to_build_a_proof_from_a_tuple():
    assert not hasattr(LibraryIdentity, "from_proof")
    assert "from_proof" not in vars(library_identity)
