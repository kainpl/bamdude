"""The connector submission rules (OrcaBamDude spec r2 §7-8, C1-C3) on a disposable embedded PostgreSQL."""

import pytest
from sqlalchemy import event

from backend.app.core.database import _strip_tz_from_params
from backend.tests.integration.test_connector_submissions import (  # noqa: F401
    _clean_spool_state,
    farm,
    sessions,
    test_a_disabled_key_publishes_nothing,
    test_a_foreign_key_sees_no_operation_and_cannot_cancel_it,
    test_a_key_restricted_to_other_printers_publishes_nothing,
    test_a_key_whose_id_was_reused_is_not_the_actor,
    test_a_printer_whose_id_was_reused_receives_nothing,
    test_a_restricted_key_cannot_use_the_auto_queue,
    test_a_retryable_entry_that_published_meanwhile_is_not_published_twice,
    test_a_writer_that_raised_after_its_commit_leaves_the_entry_published,
    test_an_archived_printer_receives_nothing,
    test_an_auto_entry_publishes_into_the_auto_queue,
    test_an_entry_publishes_its_quantity_and_the_receipt_names_the_jobs,
    test_an_operation_made_for_another_server_is_refused,
    test_bytes_other_than_the_uploaded_ones_publish_nothing,
    test_cancel_fences_unpublished_entries_only,
    test_running_a_published_operation_again_creates_no_job,
    test_the_same_operation_with_other_content_is_a_conflict,
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
