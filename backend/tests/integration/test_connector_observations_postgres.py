"""Observations by identities (OrcaBamDude host spec r2.2 §11 S3) and the connector's write locks, on a
disposable embedded PostgreSQL."""

import pytest
from sqlalchemy import event

from backend.app.core.database import _strip_tz_from_params
from backend.tests.integration.test_connector_observations import (  # noqa: F401
    _clean_spool_state,
    farm,
    runtime,
    sessions,
    test_a_cancel_writes_only_the_operation_it_proved,
    test_a_completed_label_is_no_first_layer_observation,
    test_a_direct_job_moved_into_the_auto_queue_is_unavailable,
    test_a_finished_job_is_read_from_its_execution_after_the_row_is_gone,
    test_a_foreign_or_damaged_cursor_is_refused,
    test_a_large_operation_is_paged_and_its_tail_is_reachable,
    test_a_prepared_execution_borrows_no_layer_and_has_not_started,
    test_a_printer_outside_the_keys_list_is_not_shown,
    test_a_queued_direct_job_names_its_printer,
    test_a_read_during_a_restore_hold_is_refused,
    test_a_receipt_without_identities_is_unavailable_and_stays_published,
    test_a_replayed_post_never_answers_with_another_operation,
    test_a_request_that_gave_up_keeps_its_lease_until_its_write_ends,
    test_a_restore_between_the_gate_and_the_answer_is_refused,
    test_a_restore_waits_for_a_cancel_until_its_write_is_done,
    test_a_restore_waits_for_a_post_until_its_answer_is_read,
    test_a_restore_waits_for_a_resume_until_its_write_is_done,
    test_a_restore_window_never_answers_with_another_operation,
    test_a_resume_writes_only_the_operation_it_proved,
    test_a_reused_id_never_lends_another_job_its_print,
    test_a_running_execution_proves_its_first_layer_from_the_live_printer,
    test_a_state_without_a_connected_flag_is_no_observation,
    test_a_submission_gone_after_the_proof_is_refused_not_a_crash,
    test_an_auto_job_finishes_through_its_lineage_after_its_rows_are_gone,
    test_an_auto_job_names_each_assignment_by_its_own_identity,
    test_every_reader_and_writer_by_id_checks_the_proof,
    test_executions_are_newest_first_and_bounded,
    test_first_layer_needs_a_printer_connected_now,
    test_first_layer_needs_a_proven_binding_to_this_very_execution,
)
from backend.tests.integration.test_connector_write_locks import (  # noqa: F401
    test_a_cancel_holds_its_row_from_the_check_to_the_write,
    test_a_resume_holds_its_row_from_the_check_to_the_write,
)
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_printer_status_batch_postgres import test_engine  # noqa: F401

pytest.importorskip("embedded_postgres")
pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(autouse=True)
def postgres_dates(test_engine):  # noqa: F811
    event.listen(test_engine.sync_engine, "before_cursor_execute", _strip_tz_from_params, retval=True)
    yield
    event.remove(test_engine.sync_engine, "before_cursor_execute", _strip_tz_from_params)
