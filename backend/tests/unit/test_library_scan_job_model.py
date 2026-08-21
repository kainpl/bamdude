"""Schema-level guarantees for a scan job."""

from __future__ import annotations

from backend.app.models.library_scan import LibraryScanJob

COUNTERS = (
    "files_total",
    "files_seen",
    "files_added",
    "files_updated",
    "files_removed",
    "folders_added",
    "folders_removed",
)


def test_a_job_starts_queued():
    assert LibraryScanJob.__table__.c.status.default.arg == "queued"


def test_the_ddl_asks_for_a_cascade():
    """⚠️ Asked for, and honoured only by PostgreSQL. SQLite does not enforce
    foreign keys unless ``PRAGMA foreign_keys = ON``, which this codebase never
    sets — see test_a_deleted_folder_leaves_its_scan_history_harmless for what
    actually happens there.
    """
    fk = next(iter(LibraryScanJob.__table__.c.folder_id.foreign_keys))
    assert fk.ondelete == "CASCADE"


def test_every_counter_starts_at_zero_rather_than_null():
    """⚠️ A job that has done nothing has done zero. NULL renders as "null files"
    and poisons any sum taken over the column.
    """
    for name in COUNTERS:
        assert LibraryScanJob.__table__.c[name].default.arg == 0, name


def test_a_skipped_deletion_is_recorded_rather_than_implied():
    """⚠️ "Nothing was removed" and "removal was refused because the mount looked
    unreachable" are different answers, and only one of them means the operator
    should go and look at their NAS. Silence would read as the first.
    """
    assert LibraryScanJob.__table__.c.skipped_deletions.default.arg is False


def test_the_folder_and_status_pair_is_indexed():
    """It is the question asked on every start — is one already running here."""
    names = {index.name for index in LibraryScanJob.__table__.indexes}
    assert "ix_library_scan_jobs_folder_status" in names


def test_scanning_gains_no_permission_of_its_own():
    """⚠️ Starting a scan is already gated by LIBRARY_UPLOAD. A second permission
    for watching one you were allowed to start gates nothing, and every new
    Permission costs a slot in two API-key maps and a seed in its migration.
    """
    from backend.app.migrations import m148_library_scan_jobs as migration

    assert not hasattr(migration, "NEW_PERMISSIONS")
    assert not hasattr(migration, "seed")
