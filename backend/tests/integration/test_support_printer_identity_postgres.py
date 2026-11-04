"""One index per machine on a disposable PostgreSQL (spec gh109-o2 r2 A01).

PostgreSQL returns an unordered SELECT in heap order, which an UPDATE moves -- the shape that put a printer's queue
row under another printer's index. The roster is read by id, so every section agrees here too.
"""

import pytest
from sqlalchemy import event, update

from backend.app.core.database import _strip_tz_from_params
from backend.app.models.printer import Printer
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_printer_status_batch_postgres import test_engine  # noqa: F401
from backend.tests.integration.test_support_printer_identity import (  # noqa: F401
    _assert_one_machine_per_index,
    _farm,
    _no_network,
    _zip,
    support_env,
    test_a_printer_deleted_and_one_added_during_the_collection_shift_nothing,
    test_a_reused_id_never_lends_its_new_state_to_the_old_index,
    test_inactive_archived_and_queueless_printers_shift_nobody,
)

pytest.importorskip("embedded_postgres")
pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(autouse=True)
def postgres_dates(test_engine):  # noqa: F811
    event.listen(test_engine.sync_engine, "before_cursor_execute", _strip_tz_from_params, retval=True)
    yield
    event.remove(test_engine.sync_engine, "before_cursor_execute", _strip_tz_from_params)


async def test_a_printer_moved_in_the_heap_keeps_its_index_on_postgres(
    db_session,
    printer_factory,
    support_env,
    monkeypatch,  # noqa: F811
):
    farm = await _farm(db_session, printer_factory, monkeypatch, with_holes=False)
    # An UPDATE writes a new tuple at the end of the heap: an unordered SELECT now returns the first printer last.
    await db_session.execute(update(Printer).where(Printer.id == farm[0].id).values(nozzle_count=2))
    await db_session.commit()

    info, files, _ = await _zip(support_env)

    _assert_one_machine_per_index(info, farm, files)
