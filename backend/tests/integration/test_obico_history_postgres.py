"""Run the same durable storage/lifecycle contracts on a disposable PostgreSQL."""

from backend.tests.unit.services.test_obico_history import (  # noqa: F401
    history,
    test_age_failed_unlink_pin_budget_invalid_jpeg_and_off,
    test_cancel_keeps_writer_reservation_and_restore_refuses_until_thread_ends,
    test_encoded_dimensions_ignore_all_exif_rotations,
    test_historical_migration_creates_a_writable_table,
    test_original_raw_geometry_restart_and_idempotence,
    test_p1_pin_registration_waits_between_no_pin_check_and_unlink,
    test_pin_delete_reuse_and_rollback,
    test_pin_preserves_dump_files_during_clear_and_records_continue,
    test_portable_restore_roundtrip,
    test_publication_faults_never_leave_ready_missing_files,
    test_safe_cannot_evict_higher_classes_and_oldest_in_class,
    test_scoped_keyset_before_page_cursor_and_counts,
    test_startup_reconciles_orphans_incomplete_and_missing_references,
)
