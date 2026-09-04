"""The operator's saved print options, as keyword arguments for a queue writer.

The print dialog reads ``print_options_preferences`` before it builds a queue
payload — swap macros, the calibration tri-states, which event macros run. Every
door that queues work WITHOUT that dialog in front of it (today the order plan's
``POST /projects/{id}/plan/enqueue``) otherwise writes the writers' own defaults,
which is how a farm configured to run swap macros printed without them, and
without ever saying so (reported 2026-09-04).

⚠️ **This is the dialog's MAPPING, not the dialog's gates.** Whether the target
printer has swap mode on, and whether the source file already carries swap
macros baked in by third-party tooling, are questions about a printer and a
file — the caller knows both and mutes the fields itself, exactly as
``queue_add`` and the two print-now routes do. Answering them here would need
this helper to take a file it has no other use for.

⚠️ Returns ``{}`` when nothing is saved, deliberately: an empty mapping unpacks
into a writer call and leaves every one of its own defaults alone. A dict of
``None``s would not — it would overwrite them.
"""

from __future__ import annotations

import logging

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.macro import Macro
from backend.app.models.print_options_preference import PrintOptionsPreference
from backend.app.models.user import User
from backend.app.schemas.print_options_preference import PrintOptionsPreferenceData
from backend.app.services.macro_matcher import _macro_targets_model

logger = logging.getLogger(__name__)

# Swap macros are not event macros: they have their own two fields on every
# queue row and their own trigger inside the dispatcher, and the dialog leaves
# them out of the event-macro list for the same reason.
_SWAP_EVENT_PREFIX = "swap_mode_"


async def preference_options(
    db: AsyncSession,
    user: User | None,
    printer_model: str | None,
) -> dict:
    """What the print dialog would have sent for ``(user, printer_model)``.

    Keys are named for the two writers that take them —
    ``services/queue_batch.py::enqueue_batch_copies`` and
    ``schemas/auto_queue.py::AutoQueueItemCreate`` — so the result unpacks into
    either. An empty dict means "nothing saved"; see the module docstring.
    """
    pref = await _saved_preference(db, user, printer_model)
    if pref is None:
        return {}
    try:
        data = PrintOptionsPreferenceData.model_validate(pref.options)
    except ValidationError:
        # A preference nobody can read is not a reason to refuse somebody's
        # print — fall back to the writers' defaults and say so once.
        logger.warning("Print-options preference %s did not parse; queueing with the writers' defaults", pref.id)
        return {}

    events = list(data.swap_macros.events)
    # Both halves, as the dialog sends them: "run swap macros" with nothing
    # ticked is not a request to run anything.
    execute_swap = bool(data.swap_macros.execute and events)
    return {
        "bed_levelling": data.print_options.bed_levelling,
        "flow_cali": data.print_options.flow_cali,
        "execute_swap_macros": execute_swap,
        "swap_macro_events": events if execute_swap else None,
        "selected_macro_ids": await _selected_macro_ids(db, printer_model, set(data.event_macros.deselected_ids)),
    }


async def _saved_preference(
    db: AsyncSession,
    user: User | None,
    printer_model: str | None,
) -> PrintOptionsPreference | None:
    """The operator's row, else the system fallback row.

    The two halves each already exist: the dialog reads the per-user row
    (``GET /print-option-preferences/{model}``), and the virtual-printer
    queue-receive path reads the ``user_id IS NULL`` system row when there is no
    user to ask. A door that has a user AND a fallback consults them in that
    order.

    ⚠️ ``printer_model=None`` is the auto-queue target: it names no machine, so
    there is no model to key by, and the row is picked by recency instead. That
    is not a guess about the model — it is the operator's most recent answer to
    exactly these questions, which is what the dialog would have shown them. The
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

    ⚠️ With no model named (the auto-queue target) every enabled macro is
    offered. That is safe rather than optimistic: ``macro_trigger`` re-filters
    the list against the printer the job actually lands on, so a superset can
    only ever fire what that printer matches anyway.
    """
    macros = (await db.execute(select(Macro).where(Macro.enabled.is_(True)))).scalars().all()
    return [
        macro.id
        for macro in macros
        if not macro.event.startswith(_SWAP_EVENT_PREFIX)
        and macro.id not in deselected
        # Reusing the matcher's own parser rather than re-reading the JSON
        # column here: its three failure modes are decided in one place, and a
        # second reading of the same column is exactly the drift that makes a
        # macro fire in one door and not the other.
        and (not printer_model or _macro_targets_model(macro, printer_model))
    ]
