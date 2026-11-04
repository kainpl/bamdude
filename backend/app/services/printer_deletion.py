"""The process-memory life of a printer record that is being deleted (spec gh109-o1 r2).

SQLite gives a deleted printer's id -- and the deleted archives' ids -- to the next rows created, so whatever this
process still keeps under the id would belong to the printer added next: its first dispatch met the old current run
(the GH109 veto), its queue waited behind the old plate gate. The route deletes the rows; this module owns what the
process keeps, in two steps around the commit:

* :func:`begin` -- before the deletion's first await: the record life and the client session are retired, the old
  effects revoked, and the boundary the cleanup keeps to is captured;
* :func:`finish` -- with no await after the commit returned: the deletion ends either way, and only a CONFIRMED
  commit removes the deleted record's runtime. A commit that raised proves nothing, and neither would a SELECT by id
  (the id may already be reused): the binding then stays for the manual GH109 repair.

Run-scoped helpers stay in ``print_run_binding``; this is only the orchestration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PrinterDeletion:
    printer_id: int
    life: int
    #: The effect generation the deletion's own ``revoke_print_effects`` set: older activity is the deleted record's.
    effect_boundary: int
    #: Start resolutions (and the terminals they buffered) up to this sequence are the deleted record's.
    resolution_boundary: int


def begin(printer_id: int) -> PrinterDeletion:
    """Retire the record's life before anything awaits. Synchronous by design."""
    from backend.app.services.print_run_binding import effect_generation, revoke_print_effects
    from backend.app.services.printer_manager import printer_manager

    life = printer_manager.begin_printer_deletion(printer_id)
    revoke_print_effects(printer_manager, printer_id)
    return PrinterDeletion(
        printer_id=printer_id,
        life=life,
        effect_boundary=effect_generation(printer_manager, printer_id) or 0,
        resolution_boundary=getattr(printer_manager, "_print_start_resolution_sequence", 0),
    )


def finish(deletion: PrinterDeletion, *, committed: bool) -> None:
    """End the deletion; when its commit is confirmed, remove the deleted record's runtime. Synchronous by design."""
    from backend.app.services.print_run_binding import retire_printer_runtime
    from backend.app.services.printer_manager import printer_manager

    printer_id = deletion.printer_id
    printer_manager.end_printer_deletion(printer_id, deletion.life)
    if not committed:
        return
    retire_printer_runtime(
        printer_manager,
        printer_id,
        effect_boundary=deletion.effect_boundary,
        resolution_boundary=deletion.resolution_boundary,
    )
    printer_manager.forget_deleted_printer(printer_id)

    from backend.app.main import forget_deleted_printer_runtime
    from backend.app.services import layer_timelapse, preheat, print_usage_journal, usage_tracker

    forget_deleted_printer_runtime(printer_id)
    layer_timelapse.cancel_session(printer_id)
    preheat.clear_pin(printer_id)
    print_usage_journal.forget_tail_state(printer_id)
    usage_tracker.forget_deleted_printer(printer_id)
    logger.info("Printer %s deleted: its runtime is gone, the next printer with this id starts clean", printer_id)
