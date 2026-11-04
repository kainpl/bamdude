"""The process-memory life of a printer record that is being deleted (spec gh109-o1 r2).

SQLite gives a deleted printer's id -- and the deleted archives' ids -- to the next rows created, so whatever this
process still keeps under the id would belong to the printer added next: its first dispatch met the old current run
(the GH109 veto), its queue waited behind the old plate gate. The route deletes the rows; this module owns what the
process keeps, in two steps around the commit:

* :func:`begin` -- before the deletion's first await: the record life and the client session are retired, the old
  effects revoked, the boundary the run cleanup keeps to captured, and every id-keyed registry without a generation
  of its own TAKEN OUT of its container, while no other life can exist under the id yet;
* :func:`finish` -- with no await after the commit returned: the deletion ends either way. A CONFIRMED commit drops
  what was taken out and the runs older than the boundary; anything else -- a commit that raised, a refusal half
  way -- puts it all back for the printer that is still there and keeps its runs for the manual GH109 repair.

So nothing is ever removed by id after the commit: a printer created with the reused id in between keeps every
registration it made (Codex acceptance O1-F1). Run-scoped helpers stay in ``print_run_binding``.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Containers keyed by the printer's id (or by a tuple that starts with it, or a ``"<id>:..."`` string) that carry
#: no generation of their own, as ``(module, attribute path)``. Taken out by :func:`begin`, put back or dropped by
#: :func:`finish`. A new one belongs here when a deleted printer's value would mislead the next printer.
_ID_KEYED = (
    ("backend.app.services.printer_manager", "printer_manager._awaiting_plate_clear"),
    ("backend.app.services.printer_manager", "printer_manager._last_trays"),
    ("backend.app.services.printer_manager", "printer_manager._current_print_user"),
    ("backend.app.main", "_active_prints"),
    ("backend.app.main", "_expected_prints"),
    ("backend.app.main", "_expected_print_creators"),
    ("backend.app.main", "_expected_print_registered_at"),
    ("backend.app.main", "_print_ams_mappings"),
    ("backend.app.main", "_active_swap_config"),
    ("backend.app.main", "_active_macro_selection"),
    ("backend.app.main", "_last_progress_milestone"),
    ("backend.app.main", "_first_layer_notified"),
    ("backend.app.main", "_notified_hms_errors"),
    ("backend.app.main", "_hms_last_seen"),
    ("backend.app.main", "_timelapse_baselines"),
    ("backend.app.main", "_untrusted_timelapse_baselines"),
    ("backend.app.main", "_user_stopped_printers"),
    ("backend.app.main", "_inprint_frame_bank"),
    ("backend.app.main", "_inprint_frame_bank_ts"),
    ("backend.app.main", "_stage22_finish_frames"),
    ("backend.app.main", "_last_status_broadcast"),
    ("backend.app.main", "_nozzle_count_updated"),
    ("backend.app.main", "_last_printer_state"),
    ("backend.app.main", "_last_printer_state_generation"),
    ("backend.app.main", "_pause_started_at"),
    ("backend.app.main", "_expected_pause_reasons"),
    ("backend.app.main", "_printer_last_connected"),
    ("backend.app.main", "_printer_offline_notify_tasks"),
    ("backend.app.main", "_unknown_tag_last_broadcast"),
    ("backend.app.main", "_stock_insertion_seen"),
    ("backend.app.main", "_ams_assignment_locks"),
    ("backend.app.main", "_ams_index_only_logged"),
    ("backend.app.main", "_ams_alarm_cooldown"),
    ("backend.app.services.layer_timelapse", "_active_sessions"),
    ("backend.app.services.preheat", "_pinned"),
    ("backend.app.services.usage_tracker", "_active_sessions"),
    ("backend.app.services.usage_tracker", "_tray_change_locks"),
    ("backend.app.services.print_usage_journal", "_tail_owners"),
    ("backend.app.services.print_usage_journal", "_pending_replays"),
    ("backend.app.services.print_usage_journal", "_replay_writers"),
    ("backend.app.services.print_usage_journal", "_unstated_unknowns"),
)


@dataclass(frozen=True)
class PrinterDeletion:
    printer_id: int
    life: int
    #: The effect generation the deletion's own ``revoke_print_effects`` set: older activity is the deleted record's.
    effect_boundary: int
    #: Start resolutions (and the terminals they buffered) up to this sequence are the deleted record's.
    resolution_boundary: int
    #: ``(container, what was taken out of it)`` for every id-keyed registry, in ``_ID_KEYED`` order.
    taken: tuple = field(default=())


def _container(module_name: str, path: str):
    obj = importlib.import_module(module_name)
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def _belongs(key, printer_id: int) -> bool:
    if isinstance(key, tuple):
        return bool(key) and key[0] == printer_id
    if isinstance(key, str):
        return key.startswith(f"{printer_id}:")
    return key == printer_id


def _take(container, printer_id: int):
    if isinstance(container, set):
        taken = {key for key in container if _belongs(key, printer_id)}
        container.difference_update(taken)
        return taken
    taken = {key: container[key] for key in list(container) if _belongs(key, printer_id)}
    for key in taken:
        container.pop(key, None)
    return taken


def _put_back(container, taken) -> None:
    if isinstance(container, set):
        container.update(taken)
        return
    for key, value in taken.items():
        container.setdefault(key, value)  # a value written since stays


def _drop(taken) -> None:
    """What a confirmed deletion leaves: an offline notice still waiting is cancelled, a timelapse's frames go."""
    values = taken.values() if isinstance(taken, dict) else ()
    for value in values:
        cancel = getattr(value, "cancel", None)
        if callable(cancel) and hasattr(value, "done") and not value.done():
            cancel()
        cleanup = getattr(value, "cleanup", None)
        if callable(cleanup):
            try:
                cleanup()
            except Exception:  # noqa: BLE001 - scratch files of a deleted printer; the startup sweep reclaims them
                logger.debug("cleanup of a deleted printer's leftover failed", exc_info=True)


def begin(printer_id: int) -> PrinterDeletion:
    """Retire the record's life and take its id-keyed state out before anything awaits. Synchronous by design."""
    from backend.app.services.print_run_binding import effect_generation, revoke_print_effects
    from backend.app.services.printer_manager import printer_manager

    life = printer_manager.begin_printer_deletion(printer_id)
    revoke_print_effects(printer_manager, printer_id)
    taken = tuple(
        (container, _take(container, printer_id))
        for container in (_container(module, path) for module, path in _ID_KEYED)
    )
    return PrinterDeletion(
        printer_id=printer_id,
        life=life,
        effect_boundary=effect_generation(printer_manager, printer_id) or 0,
        resolution_boundary=getattr(printer_manager, "_print_start_resolution_sequence", 0),
        taken=taken,
    )


def finish(deletion: PrinterDeletion, *, committed: bool) -> None:
    """End the deletion; when its commit is confirmed, drop the deleted record's runtime. Synchronous by design."""
    from backend.app.services.print_run_binding import retire_printer_runtime
    from backend.app.services.printer_manager import printer_manager

    printer_id = deletion.printer_id
    printer_manager.end_printer_deletion(printer_id, deletion.life)
    if not committed:
        for container, taken in deletion.taken:
            _put_back(container, taken)
        return
    retire_printer_runtime(
        printer_manager,
        printer_id,
        effect_boundary=deletion.effect_boundary,
        resolution_boundary=deletion.resolution_boundary,
    )
    for _unused, taken in deletion.taken:
        _drop(taken)
    logger.info("Printer %s deleted: its runtime is gone, the next printer with this id starts clean", printer_id)
