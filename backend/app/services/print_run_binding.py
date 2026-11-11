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


#: The origins that PROVE which run a binding addresses (swap-source-lifecycle spec r4 §7, §10): our own dispatch,
#: a new external start observed live, and a recovery by the device's unique subtask id. A run found by a name, an
#: alias or a hash twin, and an adopted one, is bound as ``observed`` -- enough to archive it, never enough for a
#: physical effect that depends on its source's declaration.
PROVEN_ORIGINS = frozenset({"dispatch", "external", "exact_subtask"})


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
    effect_generation: int = 0
    archive_identity: str | None = None

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
    subtask_id: str | None
    effect_generation: int


@dataclass(frozen=True, slots=True)
class PrintEffectToken:
    """A run's permission survives resource cleanup, never newer activity."""

    printer_id: int
    archive_id: int | None
    generation: int


@dataclass(slots=True)
class PrintStartAdmission:
    """One live start, independent of prepared/observed binding enrichment."""

    subtask_id: str | None
    archive_id: int | None
    generation: int
    phase: str = "processing"
    boundary_generation: int | None = None


def _effect_generations(manager: PrinterManager) -> dict[int, int]:
    generations = getattr(manager, "_print_effect_generations", None)
    if not isinstance(generations, dict):
        generations = {}
        manager._print_effect_generations = generations
    return generations


def _advance_activity(manager: PrinterManager, printer_id: int) -> int:
    # Never reset on discard. Even deletion/reuse of a printer id cannot
    # authorize a surviving old task: new activity gets a process-unique value.
    previous = getattr(manager, "_print_effect_sequence", 0)
    sequence = previous + 1 if isinstance(previous, int) else 1
    manager._print_effect_sequence = sequence
    _effect_generations(manager)[printer_id] = sequence
    from backend.app.services.layer_projection import clear_layer_grant

    clear_layer_grant(manager, printer_id)
    return sequence


def origin_is_proven(run: PrintRunBinding | None) -> bool:
    """Whether a binding's origin proves the run it addresses -- never inferred from a filename."""
    return run is not None and run.origin in PROVEN_ORIGINS


def effect_token_for_run(run: PrintRunBinding) -> PrintEffectToken:
    return PrintEffectToken(run.printer_id, run.archive_id, run.effect_generation)


def print_effect_is_current(manager: PrinterManager, token: PrintEffectToken) -> bool:
    return bool(
        getattr(manager, "_print_lifecycle_stopping", False) is not True
        and token.generation
        and _effect_generations(manager).get(token.printer_id) == token.generation
    )


def current_print_effect_token(manager: PrinterManager, printer_id: int) -> PrintEffectToken | None:
    run = _current(manager).get(printer_id)
    if run is not None:
        return effect_token_for_run(run)
    admission = _start_admissions(manager).get(printer_id)
    if admission is not None:
        return PrintEffectToken(printer_id, admission.archive_id, admission.generation)
    finishing = finishing_print_runs(manager, printer_id)
    if finishing:
        return effect_token_for_run(max(finishing, key=lambda run: run.effect_generation))
    return None


def _start_admissions(manager: PrinterManager) -> dict[int, PrintStartAdmission]:
    admissions = getattr(manager, "_print_start_admissions", None)
    if not isinstance(admissions, dict):
        admissions = {}
        manager._print_start_admissions = admissions
    return admissions


def claim_print_start(manager: PrinterManager, printer_id: int, data: dict) -> PrintStartAdmission | None:
    """Claim before any start effect; concurrent duplicates never claim twice."""
    if getattr(manager, "_print_lifecycle_stopping", False) is True:
        return None
    actual = _normalise_subtask_id(data.get("subtask_id"))
    run = _current(manager).get(printer_id)
    previous = _start_admissions(manager).get(printer_id)
    observation = data.get("_print_observation")
    boundary_generation = observation.generation if observation is not None and observation.new_run else None
    if previous is not None:
        same_archive = run is not None and previous.archive_id == run.archive_id
        no_conflict = not (actual and previous.subtask_id and actual != previous.subtask_id)
        same_generation = _effect_generations(manager).get(printer_id) == previous.generation
        if (
            same_generation
            and no_conflict
            and (same_archive or run is None)
            and previous.boundary_generation == boundary_generation
        ):
            if actual:
                previous.subtask_id = actual
            return None
    begin_print_start_resolution(manager, printer_id, data)
    resolution = _start_resolutions(manager)[printer_id]
    admission = PrintStartAdmission(
        actual,
        run.archive_id if run is not None and run.matches_device_subtask(actual) else None,
        resolution.effect_generation,
        boundary_generation=boundary_generation,
    )
    _start_admissions(manager)[printer_id] = admission
    return admission


def start_admission_is_current(manager: PrinterManager, printer_id: int, admission: PrintStartAdmission) -> bool:
    return _start_admissions(manager).get(printer_id) is admission and (
        _effect_generations(manager).get(printer_id) == admission.generation
    )


def finish_print_start(manager: PrinterManager, printer_id: int, admission: PrintStartAdmission) -> None:
    if admission.phase == "processing":
        admission.phase = "processed"
    resolution = _start_resolutions(manager).get(printer_id)
    if resolution is not None and resolution.effect_generation == admission.generation:
        end_print_start_resolution(manager, printer_id, resolution.sequence)


def retire_print_start(
    manager: PrinterManager,
    printer_id: int,
    archive_id: int | None,
    *,
    expected: PrintStartAdmission | None = None,
) -> None:
    admission = _start_admissions(manager).get(printer_id)
    if admission is not None and admission.archive_id == archive_id and (expected is None or admission is expected):
        _start_admissions(manager).pop(printer_id, None)


def snapshot_completion_token(manager: PrinterManager, printer_id: int) -> PrintEffectToken:
    token = current_print_effect_token(manager, printer_id)
    if token is not None:
        return token
    # Legacy/no-archive live callbacks still receive an immutable fence before
    # their first await. No archive is fabricated and nothing is replayed.
    generation = _effect_generations(manager).get(printer_id) or _advance_activity(manager, printer_id)
    return PrintEffectToken(printer_id, None, generation)


def accept_unbound_completion(manager: PrinterManager, token: PrintEffectToken) -> bool:
    """At most one live terminal for a no-archive generation; never replay it."""
    if not print_effect_is_current(manager, token):
        return False
    accepted = getattr(manager, "_print_terminal_generations", None)
    if not isinstance(accepted, dict):
        accepted = {}
        manager._print_terminal_generations = accepted
    if accepted.get(token.printer_id) == token.generation:
        return False
    accepted[token.printer_id] = token.generation
    return True


def unbound_terminal_matches_start(manager: PrinterManager, printer_id: int, data: dict) -> bool:
    """An unarchived live start still rejects a contradictory positive ID."""
    admission = _start_admissions(manager).get(printer_id)
    if admission is None:
        return True
    actual = _normalise_subtask_id(data.get("subtask_id"))
    return start_admission_is_current(manager, printer_id, admission) and not (
        actual and admission.subtask_id and actual != admission.subtask_id
    )


def revoke_print_effects(manager: PrinterManager, printer_id: int | None = None) -> None:
    if printer_id is None:
        manager._print_lifecycle_stopping = True
    printers = tuple(_effect_generations(manager)) if printer_id is None else (printer_id,)
    for key in printers:
        _advance_activity(manager, key)
        _start_admissions(manager).pop(key, None)


def retire_printer_runtime(
    manager: PrinterManager, printer_id: int, *, effect_boundary: int, resolution_boundary: int
) -> None:
    """Drop a deleted printer record's runs, starts and buffered terminals -- only theirs.

    Called once the deletion committed, with no await before it. The deletion's
    own ``revoke_print_effects`` set ``effect_boundary``; everything of the
    record it deleted is older, and a printer created since with the reused id
    gets newer activity, so it is never touched here. Nothing is reset: every
    sequence stays monotonic and no revoked token becomes current again (spec
    gh109-o1 r2 R2/R5).
    """
    current = _current(manager).get(printer_id)
    if current is not None and current.effect_generation < effect_boundary:
        _current(manager).pop(printer_id, None)
        from backend.app.services.layer_projection import clear_layer_grant

        clear_layer_grant(manager, printer_id)
    for key, run in list(_finishing(manager).items()):
        if key[0] == printer_id and run.effect_generation < effect_boundary:
            _finishing(manager).pop(key, None)
    admission = _start_admissions(manager).get(printer_id)
    if admission is not None and admission.generation < effect_boundary:
        _start_admissions(manager).pop(printer_id, None)
    resolution = _start_resolutions(manager).get(printer_id)
    if resolution is not None and resolution.sequence <= resolution_boundary:
        _start_resolutions(manager).pop(printer_id, None)
    pending = _pending_terminals(manager).get(printer_id)
    if pending:
        for sequence in [sequence for sequence in pending if sequence <= resolution_boundary]:
            pending.pop(sequence, None)
        if not pending:
            _pending_terminals(manager).pop(printer_id, None)


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
    run = _current(manager).get(printer_id)
    observation, decision = data.get("_print_observation"), data.get("_execution_decision")
    different_execution = bool(
        observation is not None
        and observation.new_run
        and decision is not None
        and (run is None or decision.current_id != run.archive_id)
    )
    generation = (
        run.effect_generation
        if run is not None and run.matches_device_subtask(subtask_id) and not different_execution
        else _advance_activity(manager, printer_id)
    )
    sequence = getattr(manager, "_print_start_resolution_sequence", 0) + 1
    manager._print_start_resolution_sequence = sequence
    _start_resolutions(manager)[printer_id] = PrintStartResolution(
        sequence=sequence, subtask_id=subtask_id, effect_generation=generation
    )
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
    if resolution is None or terminal_id is None or terminal_id != resolution.subtask_id:
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
    archive_identity: str | None = None,
) -> PrintRunBinding:
    """Bind a known run without replacing a same-archive observation.

    The caller is already on the app loop.  A repeated MQTT start may add a
    previously absent device ID or queue claim, but it does not invalidate a
    finishing handle or manufacture a new physical run.
    """

    current = _current(manager).get(printer_id)
    same_attempt = current is not None and not (
        queue_item_id is not None
        and current.queue_item_id is not None
        and queue_item_id != current.queue_item_id
        or claim_started_at is not None
        and current.claim_started_at is not None
        and claim_started_at != current.claim_started_at
    )
    if (
        current is not None
        and current.archive_id == archive_id
        and (
            archive_identity is None or current.archive_identity is None or archive_identity == current.archive_identity
        )
        and (archive_identity is None or current.archive_identity in (None, archive_identity))
        and same_attempt
        and current.matches_device_subtask(observed_subtask_id)
        and current.matches_device_subtask(expected_submission_id)
    ):
        updated = replace(
            current,
            archive_identity=archive_identity or current.archive_identity,
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
    resolution = _start_resolutions(manager).get(printer_id)
    generation = (
        resolution.effect_generation
        if resolution is not None
        and (resolution.subtask_id is None or resolution.subtask_id == _normalise_subtask_id(observed_subtask_id))
        else _advance_activity(manager, printer_id)
    )
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
        effect_generation=generation,
        archive_identity=archive_identity,
    )
    _current(manager)[printer_id] = bound
    admission = _start_admissions(manager).get(printer_id)
    if admission is not None and admission.generation == generation:
        admission.archive_id = archive_id
    return bound


def prepared_print_run_conflicts(
    manager: PrinterManager,
    printer_id: int,
    *,
    archive_id: int | None = None,
    expected_submission_id: str | None = None,
    archive_identity: str | None = None,
) -> bool:
    """Read the current-run veto without reserving or releasing anything.

    Early callers have no execution archive yet: any current run vetoes that
    new attempt. The final bind may recognise its own prepared archive. An
    early refusal can therefore outlive a short finishing race until the next
    request/tick; it never grants permission to replace an observed run.
    """
    current = _peek(manager, "_print_run_bindings").get(printer_id)
    return current is not None and (
        archive_id is None
        or current.archive_id != archive_id
        or bool(archive_identity and current.archive_identity and archive_identity != current.archive_identity)
        or bool(current.observed_subtask_id and not current.matches_device_subtask(expected_submission_id))
    )


def bind_prepared_print_run(
    manager: PrinterManager,
    *,
    printer_id: int,
    archive_id: int,
    queue_item_id: int | None = None,
    claim_started_at: datetime | None = None,
    expected_submission_id: str | None = None,
    client_generation: int | None = None,
    archive_identity: str | None = None,
) -> PrintRunBinding | None:
    """Bind an owned pre-publish attempt without displacing an observed run.

    A queued dispatch can spend time uploading/preheating while an operator
    starts another print from the screen.  The later prepared attempt has no
    authority to replace that physical run merely because it reached publish
    next.  ``None`` is the caller's signal to defer its own claim.
    """

    if prepared_print_run_conflicts(
        manager,
        printer_id,
        archive_id=archive_id,
        expected_submission_id=expected_submission_id,
        archive_identity=archive_identity,
    ):
        return None
    return bind_print_run(
        manager,
        printer_id=printer_id,
        archive_id=archive_id,
        queue_item_id=queue_item_id,
        claim_started_at=claim_started_at,
        expected_submission_id=expected_submission_id,
        client_generation=client_generation,
        archive_identity=archive_identity,
        origin="dispatch",
    )


def current_print_run(manager: PrinterManager, printer_id: int) -> PrintRunBinding | None:
    """Return the current binding only; never infer it from a filename."""

    return _current(manager).get(printer_id)


def _peek(manager: PrinterManager, name: str) -> dict:
    """A registry as it is, without creating it: diagnostics must not write even an empty dict."""
    registry = getattr(manager, name, None)
    return registry if isinstance(registry, dict) else {}


def start_admission(manager: PrinterManager, printer_id: int) -> PrintStartAdmission | None:
    """The live start claim, for read-only diagnostics. Mutable: compare its fields, not only the object."""
    return _peek(manager, "_print_start_admissions").get(printer_id)


def start_resolution(manager: PrinterManager, printer_id: int) -> PrintStartResolution | None:
    return _peek(manager, "_print_start_resolutions").get(printer_id)


def has_pending_terminal(manager: PrinterManager, printer_id: int) -> bool:
    return bool(_peek(manager, "_pending_print_terminals").get(printer_id))


def effect_generation(manager: PrinterManager, printer_id: int) -> int | None:
    """The printer's current activity, which every effect token is fenced by."""
    return _peek(manager, "_print_effect_generations").get(printer_id)


def terminal_accepted(manager: PrinterManager, printer_id: int, generation: int) -> bool:
    """Whether an unbound terminal of ``generation`` is already deduplicated (``accept_unbound_completion``)."""
    return _peek(manager, "_print_terminal_generations").get(printer_id) == generation


def has_runtime_context(manager: PrinterManager, printer_id: int) -> bool:
    """Anything of a run this process still holds for the printer: a binding, a start, a finish, a terminal."""
    return bool(
        _peek(manager, "_print_run_bindings").get(printer_id)
        or _peek(manager, "_print_start_admissions").get(printer_id)
        or _peek(manager, "_print_start_resolutions").get(printer_id)
        or _peek(manager, "_pending_print_terminals").get(printer_id)
        or any(pid == printer_id for pid, _ in _peek(manager, "_print_run_finishing_bindings"))
    )


@dataclass(frozen=True)
class RunContextExpectation:
    """The exact in-process context an addressed repair checked: the objects, and the admission's fields.

    The binding is frozen and replaced on every change, so the object itself is its version. The admission is
    mutated in place, so its fields are kept beside it (spec printer-runtime-stall r3 R7).
    """

    printer_id: int
    current: PrintRunBinding
    admission: PrintStartAdmission | None
    admission_fields: tuple | None


def capture_runtime_context(manager: PrinterManager, printer_id: int) -> RunContextExpectation | None:
    current = _peek(manager, "_print_run_bindings").get(printer_id)
    if current is None:
        return None
    admission = _peek(manager, "_print_start_admissions").get(printer_id)
    fields = (
        None
        if admission is None
        else (admission.phase, admission.subtask_id, admission.archive_id, admission.generation)
    )
    return RunContextExpectation(printer_id, current, admission, fields)


def runtime_context_unchanged(manager: PrinterManager, expected: RunContextExpectation) -> bool:
    """Whether the context is still exactly what was checked, with nothing working around it. No side effect."""
    pid = expected.printer_id
    if getattr(manager, "_print_lifecycle_stopping", False) is True:
        return False
    if _peek(manager, "_print_run_bindings").get(pid) is not expected.current:
        return False
    admission = _peek(manager, "_print_start_admissions").get(pid)
    if admission is not expected.admission:
        return False
    if admission is not None and (
        (admission.phase, admission.subtask_id, admission.archive_id, admission.generation) != expected.admission_fields
        or admission.phase != "processed"
    ):
        return False
    if pid in _peek(manager, "_print_start_resolutions") or _peek(manager, "_pending_print_terminals").get(pid):
        return False
    if any(bound == pid for bound, _ in _peek(manager, "_print_run_finishing_bindings")):
        return False
    generation = expected.current.effect_generation
    if _peek(manager, "_print_effect_generations").get(pid) != generation:
        return False
    marker = _peek(manager, "_print_terminal_generations").get(pid)
    return marker is None or marker <= generation


def release_runtime_context(manager: PrinterManager, expected: RunContextExpectation) -> None:
    """Remove the checked run and its processed start, and mark its terminal handled -- call only right after
    :func:`runtime_context_unchanged` answered True, with no await between. The activity generation is NOT
    advanced: a late unbound terminal of this run must stay a duplicate (``accept_unbound_completion``)."""
    pid = expected.printer_id
    _current(manager).pop(pid, None)
    from backend.app.services.layer_projection import clear_layer_grant

    clear_layer_grant(manager, pid)
    if expected.admission is not None:
        _start_admissions(manager).pop(pid, None)
    accepted = getattr(manager, "_print_terminal_generations", None)
    if not isinstance(accepted, dict):
        accepted = {}
        manager._print_terminal_generations = accepted
    accepted[pid] = expected.current.effect_generation


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
    run = _finishing(manager).get((printer_id, archive_id))
    if run is None or not print_effect_is_current(manager, effect_token_for_run(run)):
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
        from backend.app.services.layer_projection import clear_layer_grant

        clear_layer_grant(manager, printer_id)
        _finishing(manager)[key] = current
        retire_print_start(manager, printer_id, archive_id)
        return current
    return _finishing(manager).get(key)


def discard_print_run(
    manager: PrinterManager,
    printer_id: int,
    archive_id: int,
    *,
    sequence: int | None = None,
    expected_identity: str | None = None,
) -> None:
    """Release only the named run; a new run on the same printer survives."""

    key = (printer_id, archive_id)
    finishing = _finishing(manager).get(key)
    if (
        finishing is not None
        and (sequence is None or finishing.sequence == sequence)
        and (expected_identity is None or finishing.archive_identity == expected_identity)
    ):
        _finishing(manager).pop(key, None)
    current = _current(manager).get(printer_id)
    if (
        current is not None
        and current.archive_id == archive_id
        and (sequence is None or current.sequence == sequence)
        and (expected_identity is None or current.archive_identity == expected_identity)
    ):
        _current(manager).pop(printer_id, None)
        from backend.app.services.layer_projection import clear_layer_grant

        clear_layer_grant(manager, printer_id)
