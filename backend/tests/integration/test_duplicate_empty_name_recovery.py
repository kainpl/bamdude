"""Re-review r2 (P2-3 closure): the live resolver and decide_start(catch_up) must agree.

Desired contract (spec P5 / §5.2A): when _live_archive_for_running_print finds the sole
printing archive of a zero-id process-fresh catch-up, decide_start(catch_up=True) adopts
that same archive instead of refusing it.
"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.services.print_execution import ExecutionChanged, decide_start


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        # no subtask name in the push: the resolver applies no name filter, decide_start falls back to the path
        {"subtask_name": "", "filename": "/data/Metadata/plate_1.gcode", "subtask_id": "0"},
        {"subtask_name": None, "filename": "/sdcard/other_name.gcode", "subtask_id": ""},
    ],
)
async def test_resolver_and_catch_up_proof_agree_without_a_subtask_name(
    db_session, test_engine, printer_factory, archive_factory, monkeypatch, data
):
    from backend.app import main
    from backend.app.services.printer_manager import printer_manager

    p = await printer_factory()
    a = await archive_factory(
        printer_id=p.id,
        status="printing",
        file_path="archive/x.3mf",
        print_name="Benchy",
        filename="Benchy.3mf",
        subtask_id=None,
    )
    monkeypatch.setattr(main, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))
    monkeypatch.setattr(printer_manager, "_print_run_bindings", {}, raising=False)
    live = await main._live_archive_for_running_print(p.id, data)
    assert (live.archive_id, live.reason) == (a.id, "unambiguous_name")
    try:
        decision = await decide_start(db_session, p.id, data, catch_up=True)
        outcome = ("adopted" if decision.current_id == a.id else "other", decision.current_id)
    except ExecutionChanged as exc:
        outcome = ("refused", str(exc))
    assert outcome == ("adopted", a.id), outcome
