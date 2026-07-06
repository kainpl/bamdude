"""Archive response schemas tolerate NULL created_at (upstream #1732 / M3).

Legacy bambuddy.db-rename installs and rows copied via the SQLite<->Postgres
cross-DB import can carry ``created_at = NULL``. The list/stats/duplicate
response models used to require a ``datetime``, so a single NULL row 500'd the
whole endpoint with a Pydantic ``ResponseValidationError``. ``created_at`` is
now ``datetime | None`` on the three response models — a NULL row validates.
The m100 migration backfills the value so this stays a belt-and-suspenders
guard rather than the primary fix.
"""

from __future__ import annotations

from backend.app.schemas.archive import ArchiveDuplicate, ArchiveResponse, ArchiveSlim


def test_archive_duplicate_accepts_null_created_at():
    dup = ArchiveDuplicate(id=1, print_name="x", created_at=None, match_type="exact")
    assert dup.created_at is None


def test_archive_slim_accepts_null_created_at():
    slim = ArchiveSlim(
        id=1,
        printer_id=None,
        print_name=None,
        filename="x.3mf",
        print_time_seconds=None,
        filament_used_grams=None,
        filament_type=None,
        filament_color=None,
        status="completed",
        started_at=None,
        completed_at=None,
        cost=None,
        created_at=None,
    )
    assert slim.created_at is None


def test_archive_response_accepts_null_created_at():
    resp = ArchiveResponse(
        id=1,
        printer_id=None,
        filename="x.3mf",
        file_path="archives/x.3mf",
        file_size=0,
        content_hash=None,
        thumbnail_path=None,
        timelapse_path=None,
        print_name=None,
        print_time_seconds=None,
        filament_used_grams=None,
        filament_type=None,
        filament_color=None,
        layer_height=None,
        nozzle_diameter=None,
        bed_temperature=None,
        nozzle_temperature=None,
        status="completed",
        started_at=None,
        completed_at=None,
        extra_data=None,
        makerworld_url=None,
        designer=None,
        is_favorite=False,
        tags=None,
        notes=None,
        cost=None,
        photos=None,
        failure_reason=None,
        created_at=None,
    )
    assert resp.created_at is None
