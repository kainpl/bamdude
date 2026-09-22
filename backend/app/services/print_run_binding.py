"""Server-only identity of one physical print run.

The printer state is a mutable MQTT projection.  It cannot be the source of
truth for a delayed terminal callback: while that callback is waiting, the
printer may already have started another print.  This module holds the small
address we already know once a run is accepted: execution archive, queue claim
and the connection observation that bound them.

It deliberately does not persist a second scheduler or lifecycle journal.
Database queue claims remain the cross-process authority; this is the
in-process fast path and the address passed to run-scoped consumers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.app.services.printer_manager import PrinterManager


@dataclass(frozen=True, slots=True)
class PrintRunBinding:
    """Immutable address of an accepted or observed physical print.

    ``archive_id`` is always the execution archive, never a source archive of
    a repeat.  ``analysis_generation`` intentionally does not belong here:
    changing a 3MF source revision must not create a new physical run.
    """

    printer_id: int
    archive_id: int
    queue_item_id: int | None = None
    claim_started_at: datetime | None = None
    # The value BamDude put on the outbound ``project_file`` command.  It is
    # an intent, not an observation: ``observed_subtask_id`` remains the value
    # the printer later echoed back.
    expected_submission_id: str | None = None
    observed_subtask_id: str | None = None
    client_generation: int | None = None
    origin: str = "observed"
    sequence: int = 0

    def matches_device_subtask(self, value: object) -> bool:
        """Reject only a positive, contradictory device identity.

        Firmware regularly omits IDs from terminal deltas.  Missing or ``0``
        is not proof of another run, but two non-zero IDs that differ are.
        """

        actual = _normalise_subtask_id(value)
        expected_values = (
            _normalise_subtask_id(self.expected_submission_id),
            _normalise_subtask_id(self.observed_subtask_id),
        )
        return not any(actual and expected and actual != expected for expected in expected_values)


def _normalise_subtask_id(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None if text != "0" else None


def _current(manager: PrinterManager) -> dict[int, PrintRunBinding]:
    bindings = getattr(manager, "_print_run_bindings", None)
    if bindings is None:
        # Small test/service doubles predate the runtime binding.  Keeping the
        # registry lazy also makes this helper safe during staged startup.
        bindings = {}
        manager._print_run_bindings = bindings
    return bindings


def _finishing(manager: PrinterManager) -> dict[tuple[int, int], PrintRunBinding]:
    bindings = getattr(manager, "_print_run_finishing_bindings", None)
    if bindings is None:
        bindings = {}
        manager._print_run_finishing_bindings = bindings
    return bindings


def bind_print_run(
    manager: PrinterManager,
    *,
    printer_id: int,
    archive_id: int,
    queue_item_id: int | None = None,
    claim_started_at: datetime | None = None,
    expected_submission_id: str | None = None,
    observed_subtask_id: str | None = None,
    client_generation: int | None = None,
    origin: str = "observed",
) -> PrintRunBinding:
    """Bind a known run without replacing a same-archive observation.

    The caller is already on the app loop.  A repeated MQTT start may add a
    previously absent device ID or queue claim, but it does not invalidate a
    finishing handle or manufacture a new physical run.
    """

    current = _current(manager).get(printer_id)
    if current is not None and current.archive_id == archive_id:
        updated = replace(
            current,
            queue_item_id=queue_item_id if queue_item_id is not None else current.queue_item_id,
            claim_started_at=claim_started_at if claim_started_at is not None else current.claim_started_at,
            expected_submission_id=(_normalise_subtask_id(expected_submission_id) or current.expected_submission_id),
            observed_subtask_id=_normalise_subtask_id(observed_subtask_id) or current.observed_subtask_id,
            client_generation=client_generation if client_generation is not None else current.client_generation,
            origin=origin if current.origin == "observed" and origin != "observed" else current.origin,
        )
        _current(manager)[printer_id] = updated
        return updated

    manager._print_run_binding_sequence = getattr(manager, "_print_run_binding_sequence", 0) + 1
    bound = PrintRunBinding(
        printer_id=printer_id,
        archive_id=archive_id,
        queue_item_id=queue_item_id,
        claim_started_at=claim_started_at,
        expected_submission_id=_normalise_subtask_id(expected_submission_id),
        observed_subtask_id=_normalise_subtask_id(observed_subtask_id),
        client_generation=client_generation,
        origin=origin,
        sequence=manager._print_run_binding_sequence,
    )
    _current(manager)[printer_id] = bound
    return bound


def current_print_run(manager: PrinterManager, printer_id: int) -> PrintRunBinding | None:
    """Return the current binding only; never infer it from a filename."""

    return _current(manager).get(printer_id)


def begin_print_run_finishing(manager: PrinterManager, printer_id: int, archive_id: int) -> PrintRunBinding | None:
    """Move exactly this run into its finishing lease.

    A late terminal callback for A cannot remove a current B: the removal is
    compare-by-archive, and finishing handles are keyed by both printer and
    archive.
    """

    key = (printer_id, archive_id)
    current = _current(manager).get(printer_id)
    if current is not None and current.archive_id == archive_id:
        _current(manager).pop(printer_id, None)
        _finishing(manager)[key] = current
        return current
    return _finishing(manager).get(key)


def discard_print_run(manager: PrinterManager, printer_id: int, archive_id: int) -> None:
    """Release only the named run; a new run on the same printer survives."""

    key = (printer_id, archive_id)
    _finishing(manager).pop(key, None)
    current = _current(manager).get(printer_id)
    if current is not None and current.archive_id == archive_id:
        _current(manager).pop(printer_id, None)
