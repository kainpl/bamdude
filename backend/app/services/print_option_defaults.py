"""The operator's saved print profile, as keyword arguments for a queue writer.

The print dialog reads ``print_options_preferences`` before it builds a queue
payload — the calibration tri-states, the recording toggles, the swap macros,
which event macros run. Every door that queues work WITHOUT that dialog in front
of it (today the order plan's ``POST /projects/{id}/plan/enqueue``) otherwise
writes the writers' own defaults, which is how a farm configured to run swap
macros printed without them, and without ever saying so (reported 2026-09-04).

⚠️ **The two writers take different sets, and that asymmetry is theirs.**
:class:`SharedQueueOptions` is what both accept; :class:`PrinterOnlyQueueOptions`
is the two more ``enqueue_batch_copies`` takes and ``AutoQueueItemCreate`` has no
field for. They are separate ``TypedDict``s rather than one dict filtered at the
call site so a key that one writer cannot take is a static error here, not a
``TypeError`` raised inside somebody's per-item write loop.

⚠️ **Two fields of the saved profile reach no writer at all**:
``preheat_override`` and ``preheat_chamber_target_override``. Neither
``AutoQueueItemCreate`` nor ``enqueue_batch_copies`` has a parameter for them —
the per-item preheat override travels on ``PrintQueueItem`` and is set by the
paths that build one directly. They are deliberately dropped here rather than
routed by some new path invented for this helper.

⚠️ **This is the dialog's MAPPING, not the dialog's gates.** Whether the target
printer has swap mode on, and whether the source file already carries swap
macros baked in by third-party tooling, are questions about a printer and a
file — the caller knows both and mutes the fields itself, exactly as
``queue_add`` and the two print-now routes do. Answering them here would need
this helper to take a file it has no other use for.

⚠️ Returns ``None`` when nothing is saved, deliberately: the caller then unpacks
nothing and every writer default stands. A profile of ``None``-valued keys would
not — it would overwrite them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TypedDict

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.macro import Macro
from backend.app.models.print_options_preference import PrintOptionsPreference
from backend.app.models.user import User
from backend.app.schemas.calibration_mode import CalibrationModeStr
from backend.app.schemas.print_options_preference import PrintOptionsPreferenceData
from backend.app.schemas.timelapse import TimelapseStorage
from backend.app.services.macro_matcher import macro_targets_model

logger = logging.getLogger(__name__)

# Swap macros are not event macros: they have their own two fields on every
# queue row and their own trigger inside the dispatcher, and the dialog leaves
# them out of the event-macro list for the same reason.
_SWAP_EVENT_PREFIX = "swap_mode_"


class SharedQueueOptions(TypedDict):
    """Every profile field BOTH queue writers take, under the names they take it.

    ⚠️ ``use_ams`` is NOT here and must not be added: it is a per-submission
    decision the dialog never reads from the preference, and the preference has
    no field for it.
    """

    bed_levelling: CalibrationModeStr
    flow_cali: CalibrationModeStr
    layer_inspect: bool
    timelapse: bool
    timelapse_storage: TimelapseStorage | None
    mesh_mode_fast_check: bool
    execute_swap_macros: bool
    swap_macro_events: list[str] | None
    selected_macro_ids: list[int]


class PrinterOnlyQueueOptions(TypedDict):
    """The two more ``enqueue_batch_copies`` takes.

    ``AutoQueueItemCreate`` has no field for either — the auto-queue row does not
    carry them, and the scheduler fills them from the printer at promotion. That
    is the writers' asymmetry, not this helper's to smooth over.
    """

    gcode_injection: bool
    nozzle_offset_cali: CalibrationModeStr


@dataclass(frozen=True)
class PrintProfile:
    """One saved profile, already split by which writer can take what."""

    shared: SharedQueueOptions
    printer_only: PrinterOnlyQueueOptions

    def for_auto_queue(self) -> dict:
        """Keyword arguments for ``AutoQueueItemCreate(**...)``."""
        return dict(self.shared)

    def for_printer_queue(self) -> dict:
        """Keyword arguments for ``enqueue_batch_copies(..., **...)``."""
        return {**self.shared, **self.printer_only}


async def preference_options(
    db: AsyncSession,
    user: User | None,
    printer_model: str | None,
) -> PrintProfile | None:
    """What the print dialog would have sent for ``(user, printer_model)``.

    ``None`` means nothing is saved for that pair; see the module docstring for
    why that is not an empty profile.
    """
    pref = await _saved_preference(db, user, printer_model)
    if pref is None:
        return None
    try:
        data = PrintOptionsPreferenceData.model_validate(pref.options)
    except ValidationError:
        # A preference nobody can read is not a reason to refuse somebody's
        # print — fall back to the writers' defaults and say so once.
        logger.warning("Print-options preference %s did not parse; queueing with the writers' defaults", pref.id)
        return None

    toggles = data.print_options
    events = list(data.swap_macros.events)
    # Both halves, as the dialog sends them: "run swap macros" with nothing
    # ticked is not a request to run anything.
    execute_swap = bool(data.swap_macros.execute and events)
    return PrintProfile(
        shared=SharedQueueOptions(
            bed_levelling=toggles.bed_levelling,
            flow_cali=toggles.flow_cali,
            layer_inspect=toggles.layer_inspect,
            timelapse=toggles.timelapse,
            timelapse_storage=toggles.timelapse_storage,
            mesh_mode_fast_check=toggles.mesh_mode_fast_check,
            execute_swap_macros=execute_swap,
            swap_macro_events=events if execute_swap else None,
            selected_macro_ids=await _selected_macro_ids(db, printer_model, set(data.event_macros.deselected_ids)),
        ),
        printer_only=PrinterOnlyQueueOptions(
            gcode_injection=toggles.gcode_injection,
            nozzle_offset_cali=toggles.nozzle_offset_cali,
        ),
    )


async def _saved_preference(
    db: AsyncSession,
    user: User | None,
    printer_model: str | None,
) -> PrintOptionsPreference | None:
    """The operator's row for that model, else the system fallback row for it.

    Two halves that already exist, consulted in order: the dialog reads the
    per-user row (``GET /print-option-preferences/{model}``), and the
    virtual-printer queue-receive path reads the ``user_id IS NULL`` system row
    when there is no user to ask. A door that has a user AND a fallback uses
    both — the dialog itself has no system step.

    ⚠️ A named model is NEVER widened to another model's row: a preference is
    per model precisely because the answers differ by machine, and borrowing one
    would be a guess dressed as a setting.

    ⚠️ ``printer_model=None`` means nothing named a model at all — a file that
    carries no ``sliced_for_model``, queued to the auto-queue. There the row is
    picked by recency instead, because the operator's most recent answer to
    exactly these questions is what the dialog would have shown them, and the
    alternative is applying nothing, which is the bug this helper exists to fix.
    """
    query = select(PrintOptionsPreference).order_by(
        PrintOptionsPreference.updated_at.desc(),
        # SQLite's CURRENT_TIMESTAMP has second resolution, so two rows saved in
        # the same second would otherwise come back in an arbitrary order.
        PrintOptionsPreference.id.desc(),
    )
    if printer_model:
        query = query.where(PrintOptionsPreference.printer_model == printer_model)
    if user is not None:
        mine = (await db.execute(query.where(PrintOptionsPreference.user_id == user.id).limit(1))).scalar_one_or_none()
        if mine is not None:
            return mine
    return (await db.execute(query.where(PrintOptionsPreference.user_id.is_(None)).limit(1))).scalar_one_or_none()


async def _selected_macro_ids(db: AsyncSession, printer_model: str | None, deselected: set[int]) -> list[int]:
    """Which event macros this print runs.

    The preference stores the EXCEPTIONS, never the selection — a macro created
    after it was saved must arrive ticked rather than silently absent, which is
    the rule the dialog applies and the only reason the stored shape is
    ``deselected_ids``.

    ⚠️ With no model named — a file that says nothing about what it was sliced
    for — every enabled macro is offered. That is safe rather than optimistic:
    ``macro_trigger`` re-filters the list against the printer the job actually
    lands on, so a superset can only ever fire what that printer matches anyway.
    """
    macros = (await db.execute(select(Macro).where(Macro.enabled.is_(True)))).scalars().all()
    return [
        macro.id
        for macro in macros
        if not macro.event.startswith(_SWAP_EVENT_PREFIX)
        and macro.id not in deselected
        and (not printer_model or macro_targets_model(macro, printer_model))
    ]
