"""Run binding is independent from filename aliases and analysis revisions."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend.app.services.print_run_binding import (
    begin_print_run_finishing,
    begin_print_start_resolution,
    bind_prepared_print_run,
    bind_print_run,
    completion_effects_are_owned,
    current_print_run,
    defer_matching_terminal_during_start,
    discard_print_run,
    end_print_start_resolution,
    finishing_print_runs,
    take_pending_terminal,
)
from backend.app.services.printer_manager import PrinterManager


def test_same_archive_enriches_binding_without_new_sequence():
    manager = PrinterManager()
    first = bind_print_run(manager, printer_id=7, archive_id=41, origin="dispatch")
    enriched = bind_print_run(
        manager,
        printer_id=7,
        archive_id=41,
        queue_item_id=13,
        claim_started_at=datetime(2026, 9, 22, tzinfo=timezone.utc),
        observed_subtask_id="73",
        client_generation=2,
        origin="own",
    )

    assert enriched.sequence == first.sequence
    assert enriched.queue_item_id == 13
    assert enriched.matches_device_subtask("73")
    assert not enriched.matches_device_subtask("74")


def test_dispatch_intent_stays_distinct_from_printer_observation():
    manager = PrinterManager()
    bound = bind_print_run(
        manager,
        printer_id=7,
        archive_id=41,
        expected_submission_id="73",
        origin="dispatch",
    )
    observed = bind_print_run(
        manager,
        printer_id=7,
        archive_id=41,
        observed_subtask_id="73",
    )

    assert bound.expected_submission_id == "73"
    assert bound.observed_subtask_id is None
    assert observed.expected_submission_id == "73"
    assert observed.observed_subtask_id == "73"
    assert observed.matches_device_subtask("73")
    assert not observed.matches_device_subtask("74")


def test_finishing_a_cannot_discard_new_current_b():
    manager = PrinterManager()
    bind_print_run(manager, printer_id=7, archive_id=41)
    finishing_a = begin_print_run_finishing(manager, 7, 41)
    bound_b = bind_print_run(manager, printer_id=7, archive_id=42)

    discard_print_run(manager, 7, 41)

    assert finishing_a is not None
    assert current_print_run(manager, 7) == bound_b


def test_finishing_registry_keeps_the_exact_terminal_run_address():
    manager = PrinterManager()
    bind_print_run(manager, printer_id=7, archive_id=41, observed_subtask_id="91")

    begin_print_run_finishing(manager, 7, 41)

    assert [run.archive_id for run in finishing_print_runs(manager, 7)] == [41]


def test_finishing_a_cannot_touch_printer_effects_after_b_starts():
    manager = PrinterManager()
    bind_print_run(manager, printer_id=7, archive_id=41)
    begin_print_run_finishing(manager, 7, 41)

    assert completion_effects_are_owned(manager, 7, 41)

    bind_print_run(manager, printer_id=7, archive_id=42)

    assert not completion_effects_are_owned(manager, 7, 41)


def test_pending_b_start_vetoes_printer_effects_for_finishing_a():
    manager = PrinterManager()
    bind_print_run(manager, printer_id=7, archive_id=41)
    begin_print_run_finishing(manager, 7, 41)
    begin_print_start_resolution(manager, 7, {"subtask_id": "92"})

    assert not completion_effects_are_owned(manager, 7, 41)


def test_pending_terminal_requires_the_active_start_subtask_id():
    manager = PrinterManager()
    sequence = begin_print_start_resolution(manager, 7, {"subtask_id": "91"})

    pending = defer_matching_terminal_during_start(manager, 7, {"subtask_id": "91", "status": "completed"})

    assert pending is not None
    assert pending.sequence == sequence
    assert defer_matching_terminal_during_start(manager, 7, {"subtask_id": "92"}) is None
    assert take_pending_terminal(manager, 7, subtask_id="92") is None
    assert take_pending_terminal(manager, 7, sequence) == pending


def test_newer_overlapping_start_survives_an_old_start_cleanup():
    manager = PrinterManager()
    old_sequence = begin_print_start_resolution(manager, 7, {"subtask_id": "91"})
    old_pending = defer_matching_terminal_during_start(manager, 7, {"subtask_id": "91"})
    new_sequence = begin_print_start_resolution(manager, 7, {"subtask_id": "92"})

    end_print_start_resolution(manager, 7, old_sequence)
    new_pending = defer_matching_terminal_during_start(manager, 7, {"subtask_id": "92"})

    assert old_pending is not None
    assert new_pending is not None
    assert new_pending.sequence == new_sequence
    assert take_pending_terminal(manager, 7, old_sequence) == old_pending
    assert take_pending_terminal(manager, 7, new_sequence) == new_pending


async def test_duplicate_terminal_in_finishing_never_enters_legacy_name_fallback(monkeypatch):
    """A duplicate A terminal must not finish through the unbound legacy path."""

    from backend.app import main

    printer_id = 991_001
    archive_id = 991_002
    bind_print_run(main.printer_manager, printer_id=printer_id, archive_id=archive_id, observed_subtask_id="91")
    begin_print_run_finishing(main.printer_manager, printer_id, archive_id)
    legacy_fallback = AsyncMock(return_value=False)
    monkeypatch.setattr(main, "_completion_conflicts_with_active_queue", legacy_fallback)

    try:
        await main._on_print_complete_impl(printer_id, {"subtask_id": "91"}, {"archive_id": None})
    finally:
        discard_print_run(main.printer_manager, printer_id, archive_id)

    legacy_fallback.assert_not_awaited()


async def test_terminal_arriving_during_matching_start_is_buffered_before_legacy_resolution(monkeypatch):
    """ID-bearing terminal waits for its start to persist an archive binding."""

    from backend.app import main

    printer_id = 991_003
    sequence = begin_print_start_resolution(main.printer_manager, printer_id, {"subtask_id": "93"})
    scheduled = []

    def capture_background(coro, **kwargs):
        scheduled.append(kwargs["name"])
        coro.close()

    monkeypatch.setattr(main, "spawn_background_task", capture_background)
    try:
        await main._on_print_complete_impl(
            printer_id, {"subtask_id": "93", "status": "completed"}, {"archive_id": None}
        )
        pending = take_pending_terminal(main.printer_manager, printer_id, sequence)
    finally:
        end_print_start_resolution(main.printer_manager, printer_id, sequence)

    assert pending is not None
    assert scheduled == [f"pending-terminal-timeout-{printer_id}-{sequence}"]


def test_archive_binding_replays_only_its_matching_pending_terminal(monkeypatch):
    """Binding an archive replays the terminal buffered for that exact run."""

    from backend.app import main
    from backend.app.services import print_file_analysis

    printer_id = 991_004
    archive = SimpleNamespace(id=991_005, subtask_id="94", file_path=None, plate_index=None)
    sequence = begin_print_start_resolution(main.printer_manager, printer_id, {"subtask_id": "94"})
    defer_matching_terminal_during_start(main.printer_manager, printer_id, {"subtask_id": "94", "status": "completed"})
    scheduled = []

    def capture_background(coro, **kwargs):
        scheduled.append(kwargs["name"])
        coro.close()

    monkeypatch.setattr(main, "spawn_background_task", capture_background)
    monkeypatch.setattr(print_file_analysis, "bind_print_file_analysis", lambda *args: None)
    try:
        main._bind_print_file_analysis_context(printer_id, archive)
    finally:
        end_print_start_resolution(main.printer_manager, printer_id, sequence)
        discard_print_run(main.printer_manager, printer_id, archive.id)

    assert take_pending_terminal(main.printer_manager, printer_id, sequence) is None
    assert scheduled == [f"replay-pending-terminal-{printer_id}-{archive.id}"]


async def test_expired_pending_terminal_replays_legacy_resolution_once(monkeypatch):
    """A start that never persists an archive cannot hold a terminal forever."""

    from backend.app import main

    printer_id = 991_006
    sequence = begin_print_start_resolution(main.printer_manager, printer_id, {"subtask_id": "95"})
    defer_matching_terminal_during_start(main.printer_manager, printer_id, {"subtask_id": "95", "status": "completed"})
    replay = AsyncMock()
    monkeypatch.setattr(main, "on_print_complete", replay)
    monkeypatch.setattr(main, "_PENDING_TERMINAL_TIMEOUT_SECONDS", 0)
    try:
        await main._replay_expired_pending_terminal(printer_id, sequence)
    finally:
        end_print_start_resolution(main.printer_manager, printer_id, sequence)

    replay.assert_awaited_once_with(
        printer_id,
        {"subtask_id": "95", "status": "completed", "_bamdude_pending_terminal_timeout": True},
    )


def test_prepared_a_cannot_replace_observed_b():
    manager = PrinterManager()
    observed_b = bind_print_run(manager, printer_id=7, archive_id=42, observed_subtask_id="92")

    prepared_a = bind_prepared_print_run(
        manager,
        printer_id=7,
        archive_id=41,
        queue_item_id=13,
        expected_submission_id="91",
    )

    assert prepared_a is None
    assert current_print_run(manager, 7) == observed_b


def test_repeated_filename_is_not_part_of_run_identity():
    manager = PrinterManager()
    first = bind_print_run(manager, printer_id=7, archive_id=41, observed_subtask_id="11")
    second = bind_print_run(manager, printer_id=7, archive_id=42, observed_subtask_id="12")

    assert first.archive_id != second.archive_id
    assert first.sequence != second.sequence
    assert current_print_run(manager, 7) == second


def test_client_generation_rejects_a_stale_callback_only_after_reconnect():
    manager = PrinterManager()

    # The manager assigns the value while it wires each client callback. A
    # queued callback can compare its captured source at app-loop ingress.
    manager._client_generations[7] = 3

    assert manager.current_client_generation(7) == 3
    assert manager.accepts_client_callback_generation(7, 3)
    assert not manager.accepts_client_callback_generation(7, 2)
    assert manager.accepts_client_callback_generation(7, None)
    assert manager.accepts_client_callback_generation(8, 1)
