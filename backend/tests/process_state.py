"""Process-wide state that a test can leave behind, and how it is emptied between tests.

Every test gets a fresh database, so printer ids repeat: printer 1 of the next test would inherit whatever printer 1
of the previous one left in memory. A plate gate left that way made the scheduler skip the next test's printer, and
42 dispatch-attempt tests failed whenever xdist ran them after ``test_interrupted_dispatch`` on one worker
(2026-10-26); an admission left the same way made operator recovery refuse every later repair (CI, 2026-10-15).

``conftest._clean_print_lifecycle_registries`` calls :func:`clean_print_lifecycle_state` around every test;
``tests/unit/test_print_registries_do_not_leak.py`` holds both lists below to account. Not a test module: conftest
and the guard import it, so it is executed once.
"""

import sys

#: Containers keyed by printer (or by tuples that start with one), as ``(module, attribute path)``. Locks and tasks
#: belong here too: one a test contended or started is bound to that test's event loop.
PRINTER_KEYED_REGISTRIES = (
    ("backend.app.services.printer_manager", "printer_manager._layer_grants"),
    ("backend.app.services.printer_manager", "printer_manager._layer_publications_pending"),
    ("backend.app.services.printer_manager", "printer_manager._unproven_starts"),
    ("backend.app.main", "_active_print_identities"),
    ("backend.app.main", "_print_ams_mapping_identities"),
    ("backend.app.main", "_last_projection_broadcast"),
    ("backend.app.main", "_projection_repeat_available"),
    ("backend.app.services.printer_manager", "printer_manager._print_start_admissions"),
    ("backend.app.services.printer_manager", "printer_manager._print_start_resolutions"),
    ("backend.app.services.printer_manager", "printer_manager._print_run_bindings"),
    ("backend.app.services.printer_manager", "printer_manager._print_run_finishing_bindings"),
    ("backend.app.services.printer_manager", "printer_manager._print_effect_generations"),
    ("backend.app.services.printer_manager", "printer_manager._print_terminal_generations"),
    ("backend.app.services.printer_manager", "printer_manager._pending_print_terminals"),
    ("backend.app.services.printer_manager", "printer_manager._awaiting_plate_clear"),
    ("backend.app.services.printer_manager", "printer_manager._current_print_user"),
    ("backend.app.services.printer_manager", "printer_manager._print_file_analysis_contexts"),
    ("backend.app.services.printer_manager", "printer_manager._print_file_analysis_finishing_contexts"),
    ("backend.app.services.printer_manager", "printer_manager._macro_waiters"),
    ("backend.app.services.printer_manager", "printer_manager._printer_lives"),
    ("backend.app.services.printer_manager", "printer_manager._record_identities"),
    ("backend.app.main", "_active_macro_selection"),
    ("backend.app.main", "_active_prints"),
    ("backend.app.main", "_active_swap_config"),
    ("backend.app.main", "_ams_alarm_cooldown"),
    ("backend.app.main", "_ams_assignment_locks"),
    ("backend.app.main", "_ams_index_only_logged"),
    ("backend.app.main", "_bed_cooldown_tasks"),
    ("backend.app.main", "_expected_pause_reasons"),
    ("backend.app.main", "_expected_print_creators"),
    ("backend.app.main", "_expected_print_registered_at"),
    ("backend.app.main", "_expected_prints"),
    ("backend.app.main", "_first_layer_notified"),
    ("backend.app.main", "_hms_last_seen"),
    ("backend.app.main", "_inprint_bank_in_flight"),
    ("backend.app.main", "_inprint_frame_bank"),
    ("backend.app.main", "_inprint_frame_bank_ts"),
    ("backend.app.main", "_last_printer_state"),
    ("backend.app.main", "_last_printer_state_generation"),
    ("backend.app.main", "_last_progress_milestone"),
    ("backend.app.main", "_last_status_broadcast"),
    ("backend.app.main", "_notified_hms_errors"),
    ("backend.app.main", "_nozzle_count_updated"),
    ("backend.app.main", "_pause_started_at"),
    ("backend.app.main", "_print_ams_mappings"),
    ("backend.app.main", "_printer_last_connected"),
    ("backend.app.main", "_printer_offline_notify_tasks"),
    ("backend.app.main", "_stage22_finish_frames"),
    ("backend.app.main", "_stage22_finish_in_flight"),
    ("backend.app.main", "_stock_insertion_seen"),
    ("backend.app.main", "_timelapse_baselines"),
    ("backend.app.main", "_unknown_tag_last_broadcast"),
    ("backend.app.main", "_untrusted_timelapse_baselines"),
    ("backend.app.main", "_user_stopped_printers"),
    ("backend.app.services.layer_timelapse", "_active_sessions"),
    ("backend.app.services.preheat", "_pinned"),
    ("backend.app.services.queue_ops", "_queue_claim_locks"),
    ("backend.app.services.usage_tracker", "_active_sessions"),
    ("backend.app.services.usage_tracker", "_tray_change_locks"),
)

#: The ``printer_manager`` containers its owners create on first use (``print_run_binding``, ``print_file_analysis``):
#: absent until then, and nothing to empty while absent. Every other entry must exist once its module is loaded.
LAZY = frozenset(
    {
        "printer_manager._layer_grants",
        "printer_manager._layer_publications_pending",
        "printer_manager._unproven_starts",
        "printer_manager._print_start_admissions",
        "printer_manager._print_start_resolutions",
        "printer_manager._print_effect_generations",
        "printer_manager._print_terminal_generations",
        "printer_manager._pending_print_terminals",
        "printer_manager._print_file_analysis_contexts",
        "printer_manager._print_file_analysis_finishing_contexts",
    }
)


def _resolve(module_name, path):
    """The container, ``None`` while its module is not loaded or a lazy one not created; anything else missing is
    a list that no longer matches the code -- an error, never a silent skip."""
    obj = sys.modules.get(module_name)
    if obj is None:
        return None
    for part in path.split("."):
        if not hasattr(obj, part):
            if path in LAZY:
                return None
            raise AttributeError(f"{module_name}:{path} no longer exists -- update tests/process_state.py")
        obj = getattr(obj, part)
    return obj


def clean_printer_keyed_registries() -> None:
    for module_name, path in PRINTER_KEYED_REGISTRIES:
        registry = _resolve(module_name, path)
        if registry is not None:
            registry.clear()


def clean_print_lifecycle_state() -> None:
    """Everything a print lifecycle in one test can leave for the next."""
    from backend.app.services import (
        background_dispatch,
        interrupted_dispatch,
        print_reconciliation,
        print_usage_journal,
    )
    from backend.app.services.printer_manager import printer_manager

    # The usage journal's tail owners are bound by the same lifecycle (post-rebuild F-R2), the attempts a runner
    # stopped watching are this process's too (D-R4), and so are the printers waiting for their
    # interrupted-dispatch recheck (I-R3).
    print_usage_journal.forget_tail_state()
    background_dispatch._unobserved_attempts.clear()
    interrupted_dispatch._waiting.clear()
    clean_printer_keyed_registries()
    if getattr(printer_manager, "_print_lifecycle_stopping", False):
        printer_manager._print_lifecycle_stopping = False
    print_reconciliation._live_completions.clear()
