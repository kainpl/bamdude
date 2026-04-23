"""Fire event-driven macros (``print_started`` etc.) on MQTT state hooks.

Called from ``main.on_print_start`` once the printer has transitioned into
``gcode_state='RUNNING'``. Loads every enabled macro for the event, filters
via :func:`macro_matcher.find_macros_for_event`, then dispatches each one
as a fire-and-forget task (so a slow gcode send or a macro delay never
blocks the surrounding print-start orchestration).

Only ``mqtt_action`` macros are supported for ``print_started`` today. A
gcode macro firing mid-print would fight the print itself — we could wire
it when there's a real use case; for now the code refuses gently and logs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from sqlalchemy import select

from backend.app.models.macro import Macro
from backend.app.models.printer import Printer
from backend.app.services.macro_executor import dispatch_mqtt_action
from backend.app.services.macro_matcher import find_macros_for_event

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.app.services.bambu_mqtt import BambuMQTTClient


logger = logging.getLogger(__name__)


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
                "[MACRO-TRIGGER] Skipping gcode macro '%s' on print_started "
                "(only mqtt_action macros are supported for this event)",
                macro.name,
            )
            return

        success, err = dispatch_mqtt_action(client, macro.mqtt_action or "", macro.name)
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

    matched = find_macros_for_event(event, printer, all_macros)
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
        asyncio.create_task(_run_one(macro, client))
