"""Manual and automatic clearance provenance on disposable PostgreSQL."""

import pytest
from sqlalchemy import event

from backend.app.core.database import _strip_tz_from_params
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_printer_status_batch_postgres import test_engine  # noqa: F401
from backend.tests.unit.services.test_order_auto_eject import (
    held,  # noqa: F401
    test_automatic_answer_records_source_and_rechecks_after_abandoned_start as exercise_automatic,
    test_existing_authenticated_manual_receipt_needs_no_data_rewrite as exercise_legacy,
    test_manual_clear_then_direct_print_with_detection_disabled_skips_camera as exercise_manual,
)

pytest.importorskip("embedded_postgres")
pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(autouse=True)
def postgres_dates(test_engine):
    event.listen(test_engine.sync_engine, "before_cursor_execute", _strip_tz_from_params, retval=True)
    yield
    event.remove(test_engine.sync_engine, "before_cursor_execute", _strip_tz_from_params)


@pytest.mark.parametrize("origin", ["direct-library", "direct-archive", "order-queue", "auto-queue"])
async def test_manual_clear_on_postgres(db_session, held, monkeypatch, origin):
    await exercise_manual(db_session, held, monkeypatch, {"auto_eject": False}, True, origin)


async def test_existing_manual_receipt_on_postgres(db_session, held, monkeypatch):
    await exercise_legacy(db_session, held, monkeypatch)


async def test_automatic_answer_retry_on_postgres(db_session, held, monkeypatch):
    await exercise_automatic(db_session, held, monkeypatch)
