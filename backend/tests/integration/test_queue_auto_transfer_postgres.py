"""The same addressed writers and barriers on a disposable PostgreSQL schema."""

from backend.tests.integration.test_queue_auto_transfer import (  # noqa: F401
    clean_spool_state,
    committing_client,
    job,
    resource_shop,
    sessions,
    shop,
    test_cancel_delete_and_transfer_share_lane,
    test_direct_preserves_blob_and_paused_error_queue,
    test_engine,
    test_existing_order_job_needs_no_new_filing_right,
    test_failure_before_commit_rolls_back_both_tiers,
    test_original_purge_and_transfer_do_not_deadlock,
    test_real_dispatch_claim_and_transfer,
    test_real_order_resource_apply_and_transfer,
    test_rearm_preserves_ancestor_plate_identity_and_history,
    test_two_applies_create_one_auto_row,
)
