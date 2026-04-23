"""Shared macro execution helpers.

Extracted from ``api/routes/macros.py::execute_macro`` so both the HTTP
endpoint and the dispatch/queue pipeline can send macros and await
printer acknowledgement without duplicating the M1002 wrapping, ACK
listener registration, and ``macro_executing`` state management.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.macro import Macro
from backend.app.models.printer import Printer
from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.services.macro_matcher import find_macros_for_event

logger = logging.getLogger(__name__)


def _idle_stg_for_model(model: str | None) -> int:
    """Return the stg_cur idle value for a given printer model.

    X1/X1C/X1E use ``-1``; every other model uses ``255``.
    """
    model_upper = (model or "").upper().replace("-", "").replace(" ", "")
    return -1 if model_upper in ("X1", "X1C", "X1E") else 255


def wrap_macro_gcode(gcode: str, model: str | None) -> str:
    """Wrap raw macro gcode with M1002 claim_action markers.

    The markers set ``stg_cur`` to signal "macro executing" at the start
    and reset it to the model-specific idle value at the end. Our MQTT
    client watches the ``stg_cur`` transition to detect completion
    (``on_macro_complete`` callback).
    """
    idle_stg = _idle_stg_for_model(model)
    return f"M1002 gcode_claim_action : 0;\nM400 S5;\n{gcode.strip()}\nM400;\nM1002 gcode_claim_action : {idle_stg};"


async def send_macro_and_await_ack(
    client: BambuMQTTClient,
    gcode: str,
    macro_name: str,
    model: str | None,
) -> tuple[bool, str]:
    """Wrap *gcode* with M1002 markers, send to *client*, wait for ACK.

    Sets ``client.state.macro_executing`` on send; clears it on failure.
    On ACK success the flag stays set — it will be cleared later when
    ``stg_cur`` transitions to idle (``on_macro_complete`` callback in
    ``bambu_mqtt.py``).

    Returns ``(success, error_message)``.
    """
    if not client or not client.state or not client.state.connected:
        return False, "Printer is not connected"

    wrapped = wrap_macro_gcode(gcode, model)
    client.state.macro_executing = macro_name

    sent = client.send_gcode(wrapped)
    if not sent:
        client.state.macro_executing = None
        logger.warning("[MACRO-EXEC] Failed to send macro '%s' — MQTT not connected", macro_name)
        return False, "Failed to send gcode — MQTT not connected"

    seq_id = str(client._sequence_id)

    # Wait for printer ACK (typically < 200 ms; 5 s hard ceiling).
    ack_event = threading.Event()
    ack_result: dict = {"success": False, "reason": ""}
    client.register_ack_listener(seq_id, ack_event, ack_result)

    try:
        await asyncio.to_thread(ack_event.wait, 5.0)
    except Exception:
        pass

    if not ack_event.is_set():
        client._ack_listeners.pop(seq_id, None)
        client.state.macro_executing = None
        logger.warning("[MACRO-EXEC] No ACK for macro '%s' (seq=%s)", macro_name, seq_id)
        return False, "Printer did not acknowledge command in time"

    if not ack_result["success"]:
        client.state.macro_executing = None
        reason = ack_result.get("reason", "unknown")
        logger.warning("[MACRO-EXEC] Printer rejected macro '%s' (seq=%s): %s", macro_name, seq_id, reason)
        return False, f"Printer rejected macro: {reason}"

    logger.info(
        "[MACRO-EXEC] ACK received — macro '%s' accepted (seq=%s)",
        macro_name,
        seq_id,
    )
    return True, ""


def dispatch_mqtt_action(
    client: BambuMQTTClient,
    mqtt_action: str,
    macro_name: str,
) -> tuple[bool, str]:
    """Execute a named MQTT action (e.g. ``chamber_light_off``) for a macro.

    Synchronous — MQTT command methods themselves publish-and-forget over
    MQTT, so there's nothing to await. Unlike gcode macros we don't wrap
    with M1002 markers (the printer doesn't ACK these).

    Returns ``(success, error_message)``.
    """
    from backend.app.core.mqtt_macro_actions import get_action

    if not client or not client.state or not client.state.connected:
        return False, "Printer is not connected"

    action = get_action(mqtt_action)
    if action is None:
        return False, f"Unknown mqtt_action: {mqtt_action}"

    try:
        ok = action.dispatch(client)
    except Exception as e:  # pragma: no cover — dispatcher is a thin adapter
        logger.exception("[MACRO-EXEC] mqtt_action '%s' for macro '%s' raised: %s", mqtt_action, macro_name, e)
        return False, f"Dispatch error: {e}"

    if not ok:
        return False, f"Printer rejected mqtt_action '{mqtt_action}'"

    logger.info("[MACRO-EXEC] mqtt_action '%s' dispatched for macro '%s'", mqtt_action, macro_name)
    return True, ""


async def find_swap_macro(
    db: AsyncSession,
    event: str,
    printer: Printer,
) -> Macro | None:
    """Find the matching enabled swap macro for *(event, printer)*.

    Queries all enabled macros for the given event, then applies
    :func:`macro_matcher.find_macros_for_event` to filter by printer
    model, ``swap_mode_only``, and ``swap_profile``. Returns the first
    match or ``None``.
    """
    result = await db.execute(
        select(Macro).where(
            Macro.event == event,
            Macro.enabled.is_(True),
        )
    )
    all_macros = list(result.scalars().all())
    if not all_macros:
        return None

    matched = find_macros_for_event(event, printer, all_macros)
    return matched[0] if matched else None
