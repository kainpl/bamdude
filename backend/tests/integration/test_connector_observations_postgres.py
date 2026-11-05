"""Observations by identities (OrcaBamDude host spec r2.2 §11 S3) on a disposable embedded PostgreSQL."""

import pytest
from sqlalchemy import event

from backend.app.core.database import _strip_tz_from_params
from backend.tests.integration.test_connector_observations import (  # noqa: F401
    _clean_spool_state,
    farm,
    sessions,
    test_a_direct_job_moved_into_the_auto_queue_is_unavailable,
    test_a_finished_job_is_read_from_its_execution_after_the_row_is_gone,
    test_a_foreign_or_damaged_cursor_is_refused,
    test_a_large_operation_is_paged_and_its_tail_is_reachable,
    test_a_printer_outside_the_keys_list_is_not_shown,
    test_a_queued_direct_job_names_its_printer,
    test_a_receipt_without_identities_is_unavailable_and_stays_published,
    test_a_restore_between_the_gate_and_the_answer_is_refused,
    test_a_reused_id_never_lends_another_job_its_print,
    test_a_running_execution_proves_its_first_layer_from_the_live_printer,
    test_an_auto_job_finishes_through_its_lineage_after_its_rows_are_gone,
    test_an_auto_job_names_each_assignment_by_its_own_identity,
    test_executions_are_newest_first_and_bounded,
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
