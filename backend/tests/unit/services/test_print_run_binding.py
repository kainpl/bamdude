"""Run binding is independent from filename aliases and analysis revisions."""

from datetime import datetime, timezone

from backend.app.services.print_run_binding import (
    begin_print_run_finishing,
    bind_prepared_print_run,
    bind_print_run,
    current_print_run,
    discard_print_run,
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
