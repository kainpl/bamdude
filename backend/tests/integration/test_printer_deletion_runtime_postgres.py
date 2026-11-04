"""The printer-deletion runtime lifecycle on a disposable PostgreSQL (spec gh109-o1 r2 A11).

PostgreSQL hands out ids from sequences and reuses none by itself, so the reuse case gives the re-added printer and
its archive the deleted ids explicitly; everything else is the SQLite module's own cases, run here.
"""

import pytest
from sqlalchemy import event

from backend.app.core.database import _strip_tz_from_params
from backend.app.services import print_run_binding as prb
from backend.app.services.printer_manager import printer_manager
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_printer_deletion_runtime import (  # noqa: F401
    _delete,
    _no_network,
    _seed_two_printers,
    test_a_commit_whose_outcome_is_not_confirmed_retires_nothing,
    test_a_delete_that_fails_part_way_retires_no_runtime,
    test_a_failed_deletion_gives_the_printer_back_its_registrations,
    test_a_missing_printer_changes_nothing,
    test_a_new_life_between_commit_and_cleanup_is_left_alone,
    test_a_new_lifes_registrations_survive_the_old_deletion,
    test_a_stale_closer_never_closes_the_archive_that_reused_the_id,
    test_after_its_archive_commit_the_closer_touches_nothing_of_the_next_printer,
    test_main_state_of_the_deleted_record_goes_and_another_printers_stays,
    test_the_closer_rechecks_the_record_after_its_write_lock,
)
from backend.tests.integration.test_printer_status_batch_postgres import test_engine  # noqa: F401

pytest.importorskip("embedded_postgres")
pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(autouse=True)
def postgres_dates(test_engine):  # noqa: F811
    event.listen(test_engine.sync_engine, "before_cursor_execute", _strip_tz_from_params, retval=True)
    yield
    event.remove(test_engine.sync_engine, "before_cursor_execute", _strip_tz_from_params)


@pytest.mark.asyncio
async def test_a_printer_readded_under_the_deleted_ids_starts_its_own_runtime_on_postgres(
    async_client, printer_factory, archive_factory
):
    kept, kept_run, doomed, current, finishing, finishing_archive_id = await _seed_two_printers(
        printer_factory, archive_factory
    )
    doomed_id = doomed.id

    response = await _delete(async_client, doomed_id)
    assert response.status_code == 200, response.text

    readded = await printer_factory(name="re-added", id=doomed_id)
    new_archive = await archive_factory(readded.id, status="printing", id=finishing_archive_id)

    assert prb.current_print_run(printer_manager, readded.id) is None
    assert prb.finishing_print_runs(printer_manager, readded.id) == ()
    assert not prb.has_runtime_context(printer_manager, readded.id)
    assert not printer_manager.is_awaiting_plate_clear(readded.id)
    new = prb.bind_prepared_print_run(
        printer_manager, printer_id=readded.id, archive_id=new_archive.id, expected_submission_id="pg"
    )
    assert new is not None and new.effect_generation > current.effect_generation
    assert prb.begin_print_run_finishing(printer_manager, readded.id, new_archive.id) is new
    assert not prb.print_effect_is_current(printer_manager, prb.effect_token_for_run(finishing))
    assert prb.current_print_run(printer_manager, kept.id) is kept_run
    prb.discard_print_run(printer_manager, readded.id, new_archive.id)
