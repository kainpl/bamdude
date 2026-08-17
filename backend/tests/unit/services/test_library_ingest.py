"""Which library row a fresh arrival becomes.

⚠️ **The contract is the feature, not the hash.** Callers must carry on with the
row this returns; one that keeps its own row has silently opted out of
deduplication and nothing will say so. That is exactly how the two upload
functions behaved before this module existed — one answered ``was_existing``,
the other ``duplicate_of``, and both created the row regardless.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.app.services.library_ingest import find_reusable_row


def _row(*, id: int, is_external: bool = False, file_path: str = "a/b.3mf") -> MagicMock:
    row = MagicMock()
    row.id = id
    row.is_external = is_external
    row.file_path = file_path
    return row


def _fake_rows(rows):
    async def _inner(db, content_hash):
        return rows

    return _inner


class TestPickingTheWinner:
    async def test_no_match_is_no_answer(self, monkeypatch) -> None:
        monkeypatch.setattr("backend.app.services.library_ingest._rows_with_hash", _fake_rows([]))
        assert await find_reusable_row(MagicMock(), content_hash="abc") is None

    async def test_a_managed_row_beats_an_external_one(self, monkeypatch) -> None:
        """The managed row is the only one that cannot vanish with a mount."""
        external, managed = _row(id=1, is_external=True), _row(id=9)
        monkeypatch.setattr("backend.app.services.library_ingest._rows_with_hash", _fake_rows([external, managed]))
        monkeypatch.setattr("backend.app.services.library_ingest._file_present", lambda row: True)

        row, present = await find_reusable_row(MagicMock(), content_hash="abc")

        assert row is managed
        assert present is True

    async def test_all_managed_falls_back_to_the_lowest_id(self, monkeypatch) -> None:
        """Oldest wins. Stable, and free to compute."""
        monkeypatch.setattr("backend.app.services.library_ingest._rows_with_hash", _fake_rows([_row(id=7), _row(id=3)]))
        monkeypatch.setattr("backend.app.services.library_ingest._file_present", lambda row: True)

        row, _ = await find_reusable_row(MagicMock(), content_hash="abc")

        assert row.id == 3

    async def test_a_missing_managed_file_is_still_the_winner(self, monkeypatch) -> None:
        """It is missing only its BYTES, and the caller is holding those — so the
        row is worth keeping, with its name, folder, notes, tags and history."""
        monkeypatch.setattr("backend.app.services.library_ingest._rows_with_hash", _fake_rows([_row(id=4)]))
        monkeypatch.setattr("backend.app.services.library_ingest._file_present", lambda row: False)

        row, present = await find_reusable_row(MagicMock(), content_hash="abc")

        assert row.id == 4
        assert present is False

    async def test_a_missing_external_file_is_no_answer_at_all(self, monkeypatch) -> None:
        """⚠️ We do not write into somebody's mount. An external row whose file is
        absent describes a mount that is not attached — not an error to correct
        from here — so the arrival becomes a managed row of its own."""
        monkeypatch.setattr(
            "backend.app.services.library_ingest._rows_with_hash", _fake_rows([_row(id=4, is_external=True)])
        )
        monkeypatch.setattr("backend.app.services.library_ingest._file_present", lambda row: False)

        assert await find_reusable_row(MagicMock(), content_hash="abc") is None

    async def test_an_absent_external_yields_to_a_managed_sibling(self, monkeypatch) -> None:
        """External sorts first only while its bytes are there. With the mount
        gone, the managed row behind it is the answer — not "no answer"."""
        absent_external, managed = _row(id=1, is_external=True), _row(id=9)
        present = {absent_external.id: False, managed.id: True}
        monkeypatch.setattr(
            "backend.app.services.library_ingest._rows_with_hash", _fake_rows([absent_external, managed])
        )
        monkeypatch.setattr("backend.app.services.library_ingest._file_present", lambda row: present[row.id])

        row, is_present = await find_reusable_row(MagicMock(), content_hash="abc")

        assert row is managed
        assert is_present is True


class TestTheExternalHashCache:
    """Re-read a mounted file only when it changed.

    The old decision was "skip hashing external files for performance", and it
    predates m129, which put the on-disk mtime on the row. With size and mtime
    already stored, the first scan of a mount pays a full read and every scan
    after it re-reads only what changed — so the performance argument no longer
    buys the hole it was paying for.
    """

    @staticmethod
    def _cached(*, file_hash, size, mtime) -> MagicMock:
        row = MagicMock()
        row.file_hash = file_hash
        row.file_size = size
        row.fs_modified_at = mtime
        return row

    def test_an_unhashed_row_is_always_stale(self) -> None:
        from backend.app.services.library_ingest import external_hash_is_stale

        assert external_hash_is_stale(self._cached(file_hash=None, size=10, mtime=5.0), size=10, mtime=5.0) is True

    def test_an_unchanged_file_is_not_re_read(self) -> None:
        from backend.app.services.library_ingest import external_hash_is_stale

        assert external_hash_is_stale(self._cached(file_hash="abc", size=10, mtime=5.0), size=10, mtime=5.0) is False

    @pytest.mark.parametrize(("size", "mtime"), [(10, 9.0), (11, 5.0), (11, 9.0)])
    def test_any_change_on_disk_is_stale(self, size: int, mtime: float) -> None:
        from backend.app.services.library_ingest import external_hash_is_stale

        assert external_hash_is_stale(self._cached(file_hash="abc", size=10, mtime=5.0), size=size, mtime=mtime) is True
