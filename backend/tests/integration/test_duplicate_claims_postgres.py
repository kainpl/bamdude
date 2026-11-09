"""Disposable PostgreSQL only; TEST_POSTGRES_URL is supplied by the runner."""

from backend.tests.integration.test_duplicate_claim_repair import (  # noqa: F401
    test_every_repeat_door_rejects_stale_execution_before_writes,
    test_group_http_rechecks_key_after_route_authorization,
    test_mcp_explains_new_actions_without_supplying_human_assertions,
    test_new_http_actions_require_assertions_and_do_not_grant_readonly_key_write,
)
from backend.tests.integration.test_duplicate_claims_wal import (  # noqa: F401
    telemetry,
    test_db_difference_cannot_promote_unproven_client,
    test_duplicate_claims_return_unresolved_without_third_row,
    test_engine,
    test_first_commit_closes_only_noncurrent_claim,
    test_group_apply_refuses_changes_and_rolls_back_whole_set,
    test_group_limit_is_complete_and_independent_of_issue_page,
    test_real_client_unproven_db_difference_never_enters_new_start,
    test_reconnect_boundary_publishes_and_retires_before_ftp,
    test_replayed_noncurrent_marker_never_books_and_closes_own_claims,
    test_settlement_then_whole_group_cancel_and_replay,
    test_waiting_claim_reads_after_the_real_database_writer,
)
from backend.tests.integration.test_duplicate_pause_seed import (  # noqa: F401
    test_first_pause_preserves_existing_seed_policy,
)
from backend.tests.integration.test_duplicate_print_execution import (  # noqa: F401
    test_adopted_current_archive_also_retires_proven_noncurrent_execution,
    test_dispatch_submission_is_durable_adoption_proof,
    test_failure_during_claim_close_rolls_back_new_and_old_archives,
    test_invalid_marker_cannot_cancel_claims,
    test_late_running_dispatch_recovers_only_its_prepared_uuid,
    test_lost_commit_acknowledgement_does_not_publish_a_second_execution,
    test_matching_name_or_hash_without_run_proof_never_adopts,
    test_reconcile_duplicate_owners_books_once_but_never_releases_one,
    test_terminal_archive_cannot_gain_a_new_active_claim,
    test_unproven_start_without_live_archive_cannot_publish,
)
from backend.tests.integration.test_duplicate_terminal import (  # noqa: F401
    make_rig,
    test_only_current_execution_completes_and_books_without_sweep,
)
