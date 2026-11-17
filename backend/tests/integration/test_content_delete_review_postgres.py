"""The reviewed production doors on an explicitly supplied disposable PG."""

import pytest

from backend.tests.integration.test_content_delete_protocol import independent_factory  # noqa: F401
from backend.tests.integration.test_content_delete_review_regressions import (
    test_a_retained_library_file_cannot_starve_its_expired_neighbours as retention,
    test_empty_trash_reports_both_deleted_and_retained_in_either_id_order as mixed_trash,
    test_facet_only_links_use_the_single_writer_and_follow_the_root_transaction as facets,
    test_finished_captured_work_requires_bytes_only_for_its_surviving_archive as finished,
    test_queue_plate_gate_is_kept_when_its_archive_is_not_in_the_user_batch as plate,
    test_queue_repair_detaches_verified_companions_and_calibration_history as queue_repair,
    test_trash_delete_reports_the_keep_instead_of_a_missing_archive as trash,
)
from backend.tests.integration.test_content_delete_user_postgres import test_engine  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("shape", ["cancelled", "failed", "completed_doomed", "completed_surviving"])
async def test_finished(async_client, db_session, printer_factory, shape):
    await finished(async_client, db_session, printer_factory, shape)


async def test_plate(async_client, db_session, printer_factory):
    await plate(async_client, db_session, printer_factory)


@pytest.mark.parametrize("keep", ["plate_gate", "current_item"])
@pytest.mark.parametrize("endpoint", ["single", "all"])
async def test_trash(async_client, db_session, printer_factory, keep, endpoint):
    await trash(async_client, db_session, printer_factory, keep, endpoint)


async def test_retention(db_session, printer_factory):
    await retention(db_session, printer_factory)


async def test_queue_repair(db_session, printer_factory):
    await queue_repair(db_session, printer_factory)


async def test_facets(db_session):
    await facets(db_session)


@pytest.mark.parametrize("retained_first", [True, False])
@pytest.mark.parametrize("keep", ["plate_gate", "current_item"])
async def test_mixed_trash(async_client, db_session, printer_factory, retained_first, keep):
    await mixed_trash(async_client, db_session, printer_factory, retained_first, keep)
