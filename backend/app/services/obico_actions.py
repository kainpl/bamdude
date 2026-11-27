"""Action dispatch for Obico failure detection.

Separated from the detection loop so actions can be unit-tested and swapped.
"""

import logging

from sqlalchemy import select

from backend.app.core.database import async_session
from backend.app.models.printer import Printer

logger = logging.getLogger(__name__)


async def execute_action(
    printer_id: int,
    action: str,
    task_name: str,
    score: float | None,
    frame: bytes | None = None,
    *,
    print_quality: int | None = None,
    printer_name: str | None = None,
) -> None:
    """Run the configured action for a detected print failure.

    action: 'notify' | 'pause' | 'pause_and_off'
    score: Obico's failure score; OctoEverywhere has none and passes print_quality (1-10).
    """
    # OctoEverywhere resolves the name before committing an action (upstream Bambuddy).
    if printer_name is None:
        printer_name = await _get_printer_name(printer_id)

    if action in ("pause", "pause_and_off"):
        _pause_print(printer_id)

    if action == "pause_and_off" and not await _turn_off_linked_plugs(printer_id):
        action = "pause"

    await _notify(printer_id, printer_name, task_name, score, action, frame, print_quality=print_quality)


async def _get_printer_name(printer_id: int) -> str:
    async with async_session() as db:
        result = await db.execute(select(Printer).where(Printer.id == printer_id))
        printer = result.scalar_one_or_none()
    return printer.name if printer else f"Printer {printer_id}"


def _pause_print(printer_id: int) -> None:
    from backend.app.services.printer_manager import printer_manager

    client = printer_manager.get_client(printer_id)
    if not client:
        logger.warning("Obico pause: no MQTT client for printer %s", printer_id)
        return
    if not client.pause_print():
        logger.warning("Obico pause: pause_print() returned False for printer %s", printer_id)


async def _turn_off_linked_plugs(printer_id: int) -> bool:
    from backend.app.services.smart_plug_manager import can_auto_turn_off, plug_switched, smart_plug_manager

    async with async_session() as db:
        plugs = await smart_plug_manager._get_plugs_for_printer(printer_id, db)
        switched_off = []
        for plug in plugs:
            # A script can only be run, never turned off: ``script/turn_off`` STOPS a script that is
            # running. The printer's "off" scripts run through plug_switched below.
            if not plug.enabled or not can_auto_turn_off(plug):
                continue
            try:
                service = await smart_plug_manager.get_service_for_plug(plug, db)
                if await service.turn_off(plug):
                    switched_off.append(plug)
                    logger.info("Obico action: turned off plug %s for printer %s", plug.name, printer_id)
                else:
                    logger.warning("Obico action: plug %s for printer %s did not switch off", plug.name, printer_id)
            except Exception as e:
                logger.error("Obico action: failed to turn off plug %s: %s", plug.name, e)
        # After every plug is off: the announce, and the "off" scripts for the plug that feeds the
        # printer — once, however many plugs feed it.
        for plug in switched_off:
            await plug_switched(plug, False, scripts="automatic")
        return any(plug.controls_printer_power for plug in switched_off)


async def _notify(
    printer_id: int,
    printer_name: str,
    task_name: str,
    score: float | None,
    action: str,
    frame: bytes | None = None,
    *,
    print_quality: int | None = None,
) -> None:
    """Fire the AI Failure Detection notification (#1794).

    Routed to its own event; previously rode the multiplexed on_printer_error
    toggle, which made it indistinguishable from HMS hardware errors in the UI
    and silently dropped Obico alerts for anyone who hadn't enabled Printer Error.
    """
    from backend.app.services.notification_service import notification_service

    async with async_session() as db:
        try:
            await notification_service.on_ai_failure_detection(
                printer_id=printer_id,
                printer_name=printer_name,
                task_name=task_name,
                confidence=score,
                action=action,
                db=db,
                # The frame the model flagged, not a fresh capture: the printer
                # has moved on by the time this is sent.
                image_data=frame,
                print_quality=print_quality,
            )
        except Exception as e:
            logger.error("Obico notify failed for printer %s: %s", printer_id, e)
