"""Fire event-driven macros on MQTT hooks.

:func:`fire_event_macros` is called from ``main.on_print_start`` once the
printer has transitioned into ``gcode_state='RUNNING'``, and from
``main.on_print_complete`` when the print reaches a terminal status.
:func:`fire_layer_macros` is called from the layer-change callback while the
print runs. Both load every enabled macro for the event, filter it through
``macro_matcher``, then dispatch each one as a fire-and-forget task (so a slow
gcode send or a macro delay never blocks the surrounding orchestration).

Only ``mqtt_action`` macros are supported today. A gcode macro firing
mid-print would fight the print itself; a gcode macro on finish is a
fair fit but not wired yet — we'll add it when there's a real use case,
for now the code refuses gently and logs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from sqlalchemy import select

from backend.app.core.tasks import spawn_background_task
from backend.app.models.archive import PrintArchive
from backend.app.models.macro import Macro
from backend.app.models.printer import Printer
from backend.app.services.archive import add_fired_layer_macro
from backend.app.services.macro_executor import dispatch_mqtt_action
from backend.app.services.macro_matcher import LAYER_REACHED_EVENT, find_layer_macros, find_macros_for_event

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.app.services.bambu_mqtt import BambuMQTTClient


logger = logging.getLogger(__name__)

# Macro ids already fired for the print currently on each printer.
# ``{printer_id: {macro_id, ...}}``. Printer id is a safe key for the same
# reason ``main._active_swap_config`` uses it — one print per printer at a
# time. This is the fast path; ``archive.extra_data['layer_macros_fired']``
# is the copy that survives a restart.
_fired_layer_macros: dict[int, set[int]] = {}


def clear_fired_layer_macros(printer_id: int) -> None:
    """Forget what fired — a new print on this printer starts with a clean sheet."""
    _fired_layer_macros.pop(printer_id, None)


async def _selected_macro_ids(db, printer_id: int) -> set[int]:
    """The macros the operator ticked for the print now on *printer_id*.

    Memory first — it is the only store that exists during the window between
    ``start_print`` and the archive being created, and ``print_started`` fires
    inside that window. The archive second, because it is the only one that
    survives a restart. An empty set when neither answers, which under opt-in
    means nothing fires.

    A registration of ``[]`` is an answer, not a miss: the operator ticked
    nothing. Falling through to the archive there would let a previous print's
    row decide this one.
    """
    from backend.app.main import _active_macro_selection

    in_memory = _active_macro_selection.get(printer_id)
    if in_memory is not None:
        return {int(i) for i in in_memory}

    archive = (
        await db.execute(
            select(PrintArchive)
            .where(PrintArchive.printer_id == printer_id, PrintArchive.status == "printing")
            .order_by(PrintArchive.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if archive is None or not isinstance(archive.extra_data, dict):
        return set()
    stored = archive.extra_data.get("selected_macro_ids")
    return {int(i) for i in stored} if isinstance(stored, list) else set()


async def _run_one(
    macro: Macro,
    client: BambuMQTTClient,
) -> None:
    """Sleep for macro.delay_seconds then dispatch. Never raises."""
    try:
        if macro.delay_seconds and macro.delay_seconds > 0:
            logger.debug(
                "[MACRO-TRIGGER] Delaying macro '%s' by %ss",
                macro.name,
                macro.delay_seconds,
            )
            await asyncio.sleep(macro.delay_seconds)

        if macro.action_type != "mqtt_action":
            logger.info(
                "[MACRO-TRIGGER] Skipping gcode macro '%s' — only mqtt_action "
                "macros are supported for event-driven triggers",
                macro.name,
            )
            return

        success, err = dispatch_mqtt_action(client, macro.mqtt_action or "", macro.name, macro.mqtt_action_param)
        if not success:
            logger.warning(
                "[MACRO-TRIGGER] macro '%s' failed: %s",
                macro.name,
                err,
            )
    except asyncio.CancelledError:
        raise
    except Exception as e:  # pragma: no cover — defensive
        logger.exception("[MACRO-TRIGGER] macro '%s' raised: %s", macro.name, e)


async def fire_event_macros(
    event: str,
    printer_id: int,
    session_factory: async_sessionmaker,
    printer_manager_module,
) -> None:
    """Load matching macros for ``(event, printer)`` and schedule each to run.

    Uses ``asyncio.create_task`` so the caller (print-start handler) doesn't
    block on ``delay_seconds`` — the macros run independently.
    """
    client = printer_manager_module.get_client(printer_id)
    if client is None or not client.state or not client.state.connected:
        logger.debug(
            "[MACRO-TRIGGER] event=%s printer=%s — no MQTT client connected, skipping",
            event,
            printer_id,
        )
        return

    async with session_factory() as db:
        printer = (await db.execute(select(Printer).where(Printer.id == printer_id))).scalar_one_or_none()
        if printer is None:
            return

        all_macros = list(
            (await db.execute(select(Macro).where(Macro.event == event, Macro.enabled.is_(True)))).scalars()
        )
        # Inside the session on purpose: the selection may have to be read off
        # the archive, which needs a session, and matching without it would
        # dispatch macros this print never asked for.
        selected = await _selected_macro_ids(db, printer_id)

    matched = [m for m in find_macros_for_event(event, printer, all_macros) if m.id in selected]
    if not matched:
        return

    logger.info(
        "[MACRO-TRIGGER] event=%s printer=%s — dispatching %d macro(s): %s",
        event,
        printer.name,
        len(matched),
        [m.name for m in matched],
    )
    for macro in matched:
        spawn_background_task(_run_one(macro, client), name=f"macro-trigger-{macro.id}")


async def fire_layer_macros(
    printer_id: int,
    layer: int,
    previous_layer: int,
    session_factory: async_sessionmaker,
    printer_manager_module,
) -> None:
    """Fire the macros whose target layer this edge just crossed.

    Two guards, for two different failure modes:

    * The **gate** — a P1S ticks ``layer_num`` during pre-print calibration,
      about half an hour before anything is printed (#1837). Only a printer in
      RUNNING with no sub-stage active is really laying down layers.
    * The **fired record** — the MQTT client is destroyed and recreated on
      every reconnect, and the replacement's state starts at layer 0, so the
      first report after a mid-print reconnect looks like a jump from 0 and
      re-crosses every target behind us.
    """
    client = printer_manager_module.get_client(printer_id)
    if client is None or not client.state or not client.state.connected:
        return

    state = client.state
    if state.state != "RUNNING" or getattr(state, "mc_print_sub_stage", 0) not in (None, 0):
        return

    fired = _fired_layer_macros.setdefault(printer_id, set())
    to_run: list[Macro] = []

    async with session_factory() as db:
        printer = (await db.execute(select(Printer).where(Printer.id == printer_id))).scalar_one_or_none()
        if printer is None:
            return

        all_macros = list(
            (
                await db.execute(select(Macro).where(Macro.event == LAYER_REACHED_EVENT, Macro.enabled.is_(True)))
            ).scalars()
        )
        selected = await _selected_macro_ids(db, printer_id)
        matched = [
            m
            for m in find_layer_macros(printer, all_macros, previous_layer, layer)
            if m.id in selected and m.id not in fired
        ]
        if not matched:
            return

        # The archive is where the record outlives a restart. If none resolves
        # we still fire on the in-memory guard alone — losing restart recovery
        # is better than losing the macro.
        archive = (
            await db.execute(
                select(PrintArchive)
                .where(PrintArchive.printer_id == printer_id, PrintArchive.status == "printing")
                .order_by(PrintArchive.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        for macro in matched:
            fired.add(macro.id)
            if archive is not None and not add_fired_layer_macro(archive, macro.id):
                logger.debug(
                    "[MACRO-LAYER] macro '%s' already fired for archive %s, skipping",
                    macro.name,
                    archive.id,
                )
                continue
            to_run.append(macro)

        if archive is not None and to_run:
            await db.commit()

    if not to_run:
        return

    logger.info(
        "[MACRO-LAYER] printer=%s crossed %s→%s — dispatching %d macro(s): %s",
        printer_id,
        previous_layer,
        layer,
        len(to_run),
        [m.name for m in to_run],
    )
    for macro in to_run:
        spawn_background_task(_run_one(macro, client), name=f"macro-layer-{macro.id}")
