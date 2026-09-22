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


@dataclass(frozen=True, slots=True)
class PendingTerminal:
    """One terminal envelope that arrived during a matching start callback."""

    sequence: int
    data: dict


@dataclass(frozen=True, slots=True)
class PrintStartResolution:
    """Identity evidence available while archive persistence is still running."""

    sequence: int
    subtask_id: str


def _normalise_subtask_id(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None if text != "0" else None


def _current(manager: PrinterManager) -> dict[int, PrintRunBinding]:
    bindings = getattr(manager, "_print_run_bindings", None)
    if not isinstance(bindings, dict):
        # Small test/service doubles predate the runtime binding.  Keeping the
        # registry lazy also makes this helper safe during staged startup.
        bindings = {}
        manager._print_run_bindings = bindings
    return bindings


def _finishing(manager: PrinterManager) -> dict[tuple[int, int], PrintRunBinding]:
    bindings = getattr(manager, "_print_run_finishing_bindings", None)
    if not isinstance(bindings, dict):
        bindings = {}
        manager._print_run_finishing_bindings = bindings
    return bindings


def _start_resolutions(manager: PrinterManager) -> dict[int, PrintStartResolution]:
    resolutions = getattr(manager, "_print_start_resolutions", None)
    if not isinstance(resolutions, dict):
        resolutions = {}
        manager._print_start_resolutions = resolutions
    return resolutions


def _pending_terminals(manager: PrinterManager) -> dict[int, dict[int, PendingTerminal]]:
    pending = getattr(manager, "_pending_print_terminals", None)
    if not isinstance(pending, dict):
        pending = {}
        manager._pending_print_terminals = pending
    return pending


def begin_print_start_resolution(manager: PrinterManager, printer_id: int, data: dict) -> int | None:
    """Mark a start that can safely buffer only its own ID-bearing terminal."""

    subtask_id = _normalise_subtask_id(data.get("subtask_id"))
    if subtask_id is None:
        return None
    sequence = getattr(manager, "_print_start_resolution_sequence", 0) + 1
    manager._print_start_resolution_sequence = sequence
    _start_resolutions(manager)[printer_id] = PrintStartResolution(sequence=sequence, subtask_id=subtask_id)
    return sequence


def end_print_start_resolution(manager: PrinterManager, printer_id: int, sequence: int | None) -> None:
    """Remove only this start; a newer overlapping callback survives."""

    resolution = _start_resolutions(manager).get(printer_id)
    if resolution is not None and resolution.sequence == sequence:
        _start_resolutions(manager).pop(printer_id, None)


def defer_matching_terminal_during_start(
    manager: PrinterManager, printer_id: int, data: dict
) -> PendingTerminal | None:
    """Save one terminal only when both callbacks name the same firmware run.

    Name-only terminals keep the legacy path: a printable name is not strong
    enough evidence to attach an old A terminal to a new B start.
    """

    resolution = _start_resolutions(manager).get(printer_id)
    terminal_id = _normalise_subtask_id(data.get("subtask_id"))
    if resolution is None or terminal_id != resolution.subtask_id:
        return None
    pending_by_sequence = _pending_terminals(manager).setdefault(printer_id, {})
    current = pending_by_sequence.get(resolution.sequence)
    if current is not None:
        return current
    pending = PendingTerminal(sequence=resolution.sequence, data=dict(data))
    pending_by_sequence[resolution.sequence] = pending
    return pending


def take_pending_terminal(
    manager: PrinterManager,
    printer_id: int,
    sequence: int | None = None,
    subtask_id: object | None = None,
) -> PendingTerminal | None:
    """Consume a buffered terminal only for its original start identity."""

    pending_by_sequence = _pending_terminals(manager).get(printer_id)
    if not pending_by_sequence or (sequence is None and subtask_id is None):
        return None
    actual_id = _normalise_subtask_id(subtask_id)
    candidates = (
        ((sequence, pending_by_sequence.get(sequence)),) if sequence is not None else tuple(pending_by_sequence.items())
    )
    for candidate_sequence, pending in candidates:
        if pending is None:
            continue
        expected_id = _normalise_subtask_id(pending.data.get("subtask_id"))
        if subtask_id is not None and actual_id != expected_id:
            continue
        pending_by_sequence.pop(candidate_sequence)
        if not pending_by_sequence:
            _pending_terminals(manager).pop(printer_id, None)
        return pending
    return None


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


def bind_prepared_print_run(
    manager: PrinterManager,
    *,
    printer_id: int,
    archive_id: int,
    queue_item_id: int | None = None,
    claim_started_at: datetime | None = None,
    expected_submission_id: str | None = None,
    client_generation: int | None = None,
) -> PrintRunBinding | None:
    """Bind an owned pre-publish attempt without displacing an observed run.

    A queued dispatch can spend time uploading/preheating while an operator
    starts another print from the screen.  The later prepared attempt has no
    authority to replace that physical run merely because it reached publish
    next.  ``None`` is the caller's signal to defer its own claim.
    """

    current = _current(manager).get(printer_id)
    if current is not None and current.archive_id != archive_id:
        return None
    return bind_print_run(
        manager,
        printer_id=printer_id,
        archive_id=archive_id,
        queue_item_id=queue_item_id,
        claim_started_at=claim_started_at,
        expected_submission_id=expected_submission_id,
        client_generation=client_generation,
        origin="dispatch",
    )


def current_print_run(manager: PrinterManager, printer_id: int) -> PrintRunBinding | None:
    """Return the current binding only; never infer it from a filename."""

    return _current(manager).get(printer_id)


def finishing_print_runs(manager: PrinterManager, printer_id: int) -> tuple[PrintRunBinding, ...]:
    """Addressed terminal leases still being processed for one printer."""

    return tuple(
        binding for (bound_printer_id, _), binding in _finishing(manager).items() if bound_printer_id == printer_id
    )


def completion_effects_are_owned(manager: PrinterManager, printer_id: int, archive_id: int) -> bool:
    """Whether a finishing run may still touch printer-wide resources.

    Archive/accounting writes remain addressed to the finishing run even after
    B appears.  Printer-wide effects are different: clearing B's macro
    selection, swapping its table, cleaning its files or powering it off is
    never a safe completion of A.  A positive current binding *or* a start
    still waiting to persist is therefore a conservative veto.
    """
    if (printer_id, archive_id) not in _finishing(manager):
        return False
    if _current(manager).get(printer_id) is not None:
        return False
    return printer_id not in _start_resolutions(manager)


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
