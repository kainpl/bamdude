import asyncio
import logging
import time
import traceback
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.printer import Printer
from backend.app.services.bambu_mqtt import BambuMQTTClient, MQTTLogEntry, PrinterState, get_stage_name

logger = logging.getLogger(__name__)

# Models that have a real chamber temperature sensor
# Based on Home Assistant Bambu Lab integration
# P1P/P1S and A1/A1Mini do NOT have chamber temp sensors
# Includes both display names and internal codes from MQTT/SSDP
CHAMBER_TEMP_SUPPORTED_MODELS = frozenset(
    [
        # Display names
        "X1",
        "X1C",
        "X1E",  # X1 series
        "X2D",  # X2 series
        "P2S",  # P2 series
        "H2C",
        "H2D",
        "H2DPRO",
        "H2S",  # H2 series
        # Internal codes (from MQTT/SSDP)
        "BL-P001",  # X1/X1C
        "C13",  # X1E
        "N6",  # X2D
        "O1D",  # H2D
        "O1C",  # H2C
        "O1C2",  # H2C (dual nozzle variant)
        "O1S",  # H2S
        "O1E",  # H2D Pro
        "O2D",  # H2D Pro (alternate code)
        "N7",  # P2S
    ]
)

# Models that may incorrectly report stg_cur=0 when idle (firmware bug)
# Based on Home Assistant Bambu Lab integration observations
# See: https://github.com/greghesp/ha-bambulab/blob/main/custom_components/bambu_lab/pybambu/models.py
A1_MODELS = frozenset(
    [
        # Display names
        "A1",
        "A1 MINI",
        "A1-MINI",
        "A1MINI",
        # Internal codes (from MQTT/SSDP)
        "N1",  # A1 Mini
        "N2S",  # A1
    ]
)

# Models affected by the stg_cur=0 idle bug (firmware reports stg_cur=0 when idle,
# which maps to "Printing" in STAGE_NAMES and overrides the correct IDLE state)
STG_CUR_IDLE_BUG_MODELS = A1_MODELS | frozenset(
    [
        # Display names
        "P1P",
        "P1S",
        # Internal codes (from MQTT/SSDP)
        "C11",  # P1P
        "C12",  # P1S
    ]
)


def supports_chamber_temp(model: str | None) -> bool:
    """Check if a printer model has a real chamber temperature sensor.

    P1P, P1S, A1, and A1Mini do NOT have chamber temp sensors.
    The 'chamber_temper' value they report is meaningless.
    """
    if not model:
        return False
    # Normalize model name (uppercase, strip whitespace)
    model_upper = model.strip().upper()
    return model_upper in CHAMBER_TEMP_SUPPORTED_MODELS


def has_stg_cur_idle_bug(model: str | None) -> bool:
    """Check if a printer model may incorrectly report stg_cur=0 when idle.

    Some firmware versions report stg_cur=0 (which maps to "Printing")
    even when the printer is idle. Originally observed on A1/A1 Mini via the
    Home Assistant Bambu Lab integration, also confirmed on P1S.
    """
    if not model:
        return False
    model_upper = model.strip().upper()
    return model_upper in STG_CUR_IDLE_BUG_MODELS


# Minimum firmware versions for AMS drying support (confirmed via capture testing)
# Keys are exact model names (upper-cased). Do NOT use substring matching - it would
# incorrectly gate X1E (matched by "X1") and H2D Pro (matched by "H2D").
_DRYING_MIN_FIRMWARE: dict[str, str] = {
    "H2D": "01.02.30.00",
    "H2S": "01.02.00.00",
    "X1": "01.09.00.00",
    "X1C": "01.09.00.00",
    "P1P": "01.08.00.00",
    "P1S": "01.08.00.00",
    "P2S": "01.02.00.00",
    "N7": "01.02.00.00",  # P2S internal model code
}
# Models that definitely don't support AMS drying (no AMS 2 Pro / AMS-HT compatibility)
_DRYING_UNSUPPORTED_MODELS = frozenset({"A1", "A1MINI", "A1-MINI", "A1 MINI", "H2C", "O1C", "O1C2", "O1S", "N1", "N2S"})


def supports_drying(model: str | None, firmware: str | None) -> bool:
    """Check if a printer model supports AMS drying commands.

    Known models with confirmed min firmware get version-gated.
    Known unsupported models are blocked.
    All other models (H2D Pro, X1E, future models) are allowed -
    the command fails gracefully with result: "fail" if unsupported.
    """
    if not model:
        return False
    model_upper = model.strip().upper()
    if model_upper in _DRYING_UNSUPPORTED_MODELS:
        return False
    if model_upper in _DRYING_MIN_FIRMWARE:
        return bool(firmware and firmware >= _DRYING_MIN_FIRMWARE[model_upper])
    # For all other models: allow
    return True


# AMS ``dry_sf_reason`` codes → human-readable blockers. Sourced from firmware
# observations in upstream #971. When one of these codes is present in an AMS
# push_status the firmware silently drops the drying command, so we surface
# them explicitly on the API route instead of returning a fake success.
DRYING_BLOCKING_REASONS: dict[int, str] = {
    0: "Printer is busy",
    1: "Insufficient power — too many AMS drying or external PSU required",
    2: "AMS is busy",
    3: "Filament is at the AMS outlet — retract it first",
    4: "AMS is already starting a drying cycle",
    5: "Not supported in 2D mode",
    6: "AMS is already drying",
    7: "AMS firmware is upgrading",
    8: "Plug in the external AMS power adapter to start drying",
}


def first_drying_blocking_reason(ams_unit: dict | None) -> tuple[int, str] | None:
    """Return the first blocking reason in an AMS unit's ``dry_sf_reason`` list.

    Returns ``(code, message)`` when at least one known blocker code is present,
    or ``None`` when the AMS is free to start drying. Unknown / malformed codes
    are skipped (fail-open) so a future firmware addition doesn't break existing
    clients that haven't been updated — they'll just see a regular drying-start
    error instead of a human-readable one.
    """
    if not ams_unit:
        return None
    for raw in ams_unit.get("dry_sf_reason") or []:
        try:
            code = int(raw)
        except (TypeError, ValueError):
            continue
        message = DRYING_BLOCKING_REASONS.get(code)
        if message:
            return code, message
    return None


def find_ams_unit(raw_data: dict | None, ams_id: int) -> dict | None:
    """Locate an AMS unit dict inside a printer push_status payload by id."""
    if not raw_data:
        return None
    for unit in raw_data.get("ams") or []:
        try:
            if int(unit.get("id", -1)) == ams_id:
                return unit
        except (TypeError, ValueError):
            continue
    return None


class PrinterInfo:
    """Basic printer info for callbacks."""

    def __init__(self, name: str, serial_number: str):
        self.name = name
        self.serial_number = serial_number


class PrinterManager:
    """Manager for multiple printer connections."""

    def __init__(self):
        self._clients: dict[int, BambuMQTTClient] = {}
        self._models: dict[int, str | None] = {}  # Cache printer models for feature detection
        self._connected_at: dict[int, float] = {}  # Unix timestamp of last connection
        self._printer_info: dict[int, PrinterInfo] = {}  # Cache printer name/serial for callbacks
        self._on_print_start: Callable[[int, dict], None] | None = None
        self._on_print_complete: Callable[[int, dict], None] | None = None
        self._on_status_change: Callable[[int, PrinterState], None] | None = None
        self._on_ams_change: Callable[[int, list], None] | None = None
        self._on_layer_change: Callable[[int, int], None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Track who started the current print (Issue #206)
        self._current_print_user: dict[int, dict] = {}  # {printer_id: {"user_id": int, "username": str}}
        # Plate-clear gate for queue flow, persisted to Printer.awaiting_plate_clear (m010, #961).
        # Semantically INVERTED vs the old _plate_cleared set: presence means the printer is
        # WAITING for user confirmation (blocked from auto-dispatch). Absence means the gate is
        # clear and the scheduler may proceed. Rehydrated from DB at startup so an Auto Off
        # power cycle can't silently bypass a pending confirmation.
        self._awaiting_plate_clear: set[int] = set()
        # Macro completion waiters: dispatch pipeline registers an Event here,
        # _broadcast_macro_complete sets it when stg_cur transitions to idle.
        self._macro_waiters: dict[int, tuple[asyncio.Event, dict]] = {}

    def get_printer(self, printer_id: int) -> PrinterInfo | None:
        """Get printer info by ID."""
        return self._printer_info.get(printer_id)

    def set_current_print_user(self, printer_id: int, user_id: int, username: str):
        """Track who started the current print (Issue #206)."""
        self._current_print_user[printer_id] = {"user_id": user_id, "username": username}

    def get_current_print_user(self, printer_id: int) -> dict | None:
        """Get the user who started the current print (Issue #206)."""
        return self._current_print_user.get(printer_id)

    def clear_current_print_user(self, printer_id: int):
        """Clear the current print user when print completes (Issue #206)."""
        self._current_print_user.pop(printer_id, None)

    def is_awaiting_plate_clear(self, printer_id: int) -> bool:
        """Returns True when the printer's queue is blocked on user plate-clear confirmation."""
        return printer_id in self._awaiting_plate_clear

    def set_awaiting_plate_clear(self, printer_id: int, awaiting: bool) -> None:
        """Arm or release the plate-clear gate. Persists to Printer.awaiting_plate_clear
        asynchronously so an Auto Off power cycle can't drop the flag (#961)."""
        if awaiting:
            self._awaiting_plate_clear.add(printer_id)
        else:
            self._awaiting_plate_clear.discard(printer_id)
        self._schedule_async(self._persist_awaiting_plate_clear(printer_id, awaiting))

    async def _persist_awaiting_plate_clear(self, printer_id: int, awaiting: bool) -> None:
        """Best-effort DB write for the awaiting-plate-clear flag. Swallows errors
        (connection issues shouldn't break the in-memory scheduler gate)."""
        try:
            from backend.app.core.database import async_session

            async with async_session() as db:
                result = await db.execute(select(Printer).where(Printer.id == printer_id))
                printer = result.scalar_one_or_none()
                if printer and printer.awaiting_plate_clear != awaiting:
                    printer.awaiting_plate_clear = awaiting
                    await db.commit()
        except Exception as e:  # pragma: no cover — persistence is best-effort
            logger.warning("Failed to persist awaiting_plate_clear for printer %s: %s", printer_id, e)

    async def load_awaiting_plate_clear_from_db(self) -> None:
        """Rehydrate the in-memory set from Printer.awaiting_plate_clear at startup (#961)."""
        from backend.app.core.database import async_session

        async with async_session() as db:
            result = await db.execute(select(Printer.id).where(Printer.awaiting_plate_clear.is_(True)))
            ids = [row[0] for row in result.all()]
        self._awaiting_plate_clear = set(ids)
        if ids:
            logger.info("Restored awaiting_plate_clear gate for %d printer(s): %s", len(ids), ids)

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        """Set the event loop for async callbacks."""
        self._loop = loop

    def set_print_start_callback(self, callback: Callable[[int, dict], None]):
        """Set callback for print start events."""
        self._on_print_start = callback

    def set_print_complete_callback(self, callback: Callable[[int, dict], None]):
        """Set callback for print completion events."""
        self._on_print_complete = callback

    def set_status_change_callback(self, callback: Callable[[int, PrinterState], None]):
        """Set callback for status change events."""
        self._on_status_change = callback

    def set_ams_change_callback(self, callback: Callable[[int, list], None]):
        """Set callback for AMS data change events."""
        self._on_ams_change = callback

    def set_layer_change_callback(self, callback: Callable[[int, int], None]):
        """Set callback for layer change events. Receives (printer_id, layer_num)."""
        self._on_layer_change = callback

    def _schedule_async(self, coro):
        """Schedule an async coroutine from a sync context.

        Captures exceptions from the coroutine and logs them to prevent
        silent failures in callbacks.
        """
        if self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)

            def handle_exception(f):
                try:
                    # This will re-raise any exception from the coroutine
                    f.result()
                except Exception as e:
                    import logging

                    logging.getLogger(__name__).error(f"Exception in scheduled callback: {e}", exc_info=True)

            future.add_done_callback(handle_exception)

    async def connect_printer(self, printer: Printer) -> bool:
        """Connect to a printer."""
        if printer.id in self._clients:
            self.disconnect_printer(printer.id)

        printer_id = printer.id

        def on_state_change(state: PrinterState):
            if self._on_status_change:
                self._schedule_async(self._on_status_change(printer_id, state))

        def on_print_start(data: dict):
            if self._on_print_start:
                self._schedule_async(self._on_print_start(printer_id, data))

        def on_print_complete(data: dict):
            if self._on_print_complete:
                self._schedule_async(self._on_print_complete(printer_id, data))

        def on_ams_change(ams_data: list):
            if self._on_ams_change:
                self._schedule_async(self._on_ams_change(printer_id, ams_data))

        def on_layer_change(layer_num: int):
            if self._on_layer_change:
                self._schedule_async(self._on_layer_change(printer_id, layer_num))

        def on_macro_complete(macro_name: str, status: str):
            self._schedule_async(self._broadcast_macro_complete(printer_id, macro_name, status))

        client = BambuMQTTClient(
            ip_address=printer.ip_address,
            serial_number=printer.serial_number,
            access_code=printer.access_code,
            model=printer.model,
            on_state_change=on_state_change,
            on_print_start=on_print_start,
            on_print_complete=on_print_complete,
            on_ams_change=on_ams_change,
            on_layer_change=on_layer_change,
            on_macro_complete=on_macro_complete,
        )

        client.connect()
        self._clients[printer_id] = client
        self._connected_at[printer_id] = time.time()
        self._models[printer_id] = printer.model  # Cache model for feature detection
        self._printer_info[printer_id] = PrinterInfo(printer.name, printer.serial_number)

        # Wait a moment for connection
        await asyncio.sleep(1)

        # Trigger a one-shot 3MF download retry for any fallback archives
        # on this printer — now that we're back online, the file may be
        # reachable.
        if client.state.connected:
            try:
                from backend.app.services.archive_download_retry import archive_download_retry

                asyncio.create_task(archive_download_retry.retry_printer_archives(printer_id))
            except Exception as e:
                logger.debug("Failed to schedule 3MF retry on printer %s connect: %s", printer_id, e)

        return client.state.connected

    def disconnect_printer(self, printer_id: int, timeout: float = 0):
        """Disconnect from a printer."""
        if printer_id in self._clients:
            self._clients[printer_id].disconnect(timeout=timeout)
            del self._clients[printer_id]
        self._connected_at.pop(printer_id, None)  # Clean up connection timestamp
        self._models.pop(printer_id, None)  # Clean up model cache
        self._printer_info.pop(printer_id, None)  # Clean up printer info cache

    def disconnect_all(self, timeout: float = 0):
        """Disconnect from all printers."""
        for printer_id in list(self._clients.keys()):
            self.disconnect_printer(printer_id, timeout=timeout)

    def get_status(self, printer_id: int) -> PrinterState | None:
        """Get the current status of a printer (checks for stale connections)."""
        if printer_id in self._clients:
            client = self._clients[printer_id]
            # Check staleness and update connected state if needed
            client.check_staleness()
            return client.state
        return None

    def get_model(self, printer_id: int) -> str | None:
        """Get the cached model for a printer."""
        return self._models.get(printer_id)

    def get_all_statuses(self) -> dict[int, PrinterState]:
        """Get status of all connected printers (checks for stale connections)."""
        result = {}
        for printer_id, client in self._clients.items():
            # Check staleness and update connected state if needed
            client.check_staleness()
            result[printer_id] = client.state
        return result

    def is_connected(self, printer_id: int) -> bool:
        """Check if a printer is connected (checks for stale connections)."""
        if printer_id in self._clients:
            client = self._clients[printer_id]
            # Check staleness and update connected state if needed
            return client.check_staleness()
        return False

    def get_client(self, printer_id: int) -> BambuMQTTClient | None:
        """Get the MQTT client for a printer."""
        return self._clients.get(printer_id)

    def get_connected_at(self, printer_id: int) -> float | None:
        """Get the unix timestamp of when the printer was last connected."""
        return self._connected_at.get(printer_id)

    async def ensure_fresh_connection(self, printer_id: int) -> bool:
        """Reconnect if MQTT connection exceeded the printer's mqtt_connection_timeout.

        Fetches the Printer from DB. Use ensure_fresh_connection_for_printer() if you already have it.
        Returns True if connection is fresh (or was refreshed), False if reconnect failed.
        """
        from backend.app.core.database import async_session

        async with async_session() as db:
            result = await db.execute(select(Printer).where(Printer.id == printer_id))
            printer = result.scalar_one_or_none()

        if not printer:
            return False

        return await self.ensure_fresh_connection_for_printer(printer)

    async def ensure_fresh_connection_for_printer(self, printer: Printer) -> bool:
        """Reconnect if MQTT connection exceeded the printer's mqtt_connection_timeout.

        Use this when you already have the Printer ORM object to avoid an extra DB query.
        Returns True if connection is fresh (or was refreshed), False if reconnect failed.
        """
        printer_id = printer.id
        connected_at = self._connected_at.get(printer_id)
        if not connected_at:
            return printer_id in self._clients

        timeout = getattr(printer, "mqtt_connection_timeout", 0)
        if timeout <= 0:
            return True  # Timeout disabled

        elapsed = time.time() - connected_at
        if elapsed <= timeout:
            return True  # Connection still fresh

        logger.info(
            "MQTT connection stale for printer %s (%.0fs > %ds), reconnecting...",
            printer.name,
            elapsed,
            timeout,
        )
        return await self.connect_printer(printer)

    def mark_printer_offline(self, printer_id: int):
        """Mark a printer as offline and trigger status callback.

        This is used when we know the printer power was cut (e.g., smart plug turned off)
        to immediately update the UI without waiting for MQTT timeout.
        """
        import logging

        logger = logging.getLogger(__name__)

        if printer_id in self._clients:
            client = self._clients[printer_id]
            if client.state.connected:
                logger.info("Marking printer %s as offline (smart plug power off)", printer_id)
                client.state.connected = False
                client.state.state = "unknown"
                # Trigger the status change callback to broadcast via WebSocket
                if self._on_status_change:
                    self._schedule_async(self._on_status_change(printer_id, client.state))

    def start_print(
        self,
        printer_id: int,
        filename: str,
        plate_id: int = 1,
        ams_mapping: list[int] | None = None,
        bed_levelling: bool = True,
        flow_cali: bool = False,
        layer_inspect: bool = False,
        timelapse: bool = False,
        use_ams: bool = True,
    ) -> bool:
        """Start a print on a connected printer."""
        caller = traceback.extract_stack(limit=3)[0]
        logger.info(
            "PRINT COMMAND: printer=%s, file=%s, caller=%s:%s:%s",
            printer_id,
            filename,
            caller.filename.split("/")[-1],
            caller.lineno,
            caller.name,
        )
        if printer_id in self._clients:
            return self._clients[printer_id].start_print(
                filename,
                plate_id,
                ams_mapping=ams_mapping,
                timelapse=timelapse,
                bed_levelling=bed_levelling,
                flow_cali=flow_cali,
                layer_inspect=layer_inspect,
                use_ams=use_ams,
            )
        return False

    async def execute_macro_and_wait(
        self,
        printer_id: int,
        gcode: str,
        macro_name: str,
    ) -> tuple[bool, str]:
        """Send a macro and block until ``on_macro_complete`` fires or the printer disconnects.

        Uses :func:`macro_executor.send_macro_and_await_ack` for the initial
        send+ACK, then waits for the ``stg_cur`` idle transition (reported by
        ``_broadcast_macro_complete``).  No fixed timeout — the printer's own
        status tracking handles errors/stalls.  A connectivity health-check
        every 0.5 s catches disconnects that wouldn't trigger a callback.

        Returns ``(success, message)``.
        """
        from backend.app.services.macro_executor import send_macro_and_await_ack

        client = self._clients.get(printer_id)
        if not client:
            return False, "Printer not connected"

        model = self._models.get(printer_id)
        ack_ok, ack_msg = await send_macro_and_await_ack(client, gcode, macro_name, model)
        if not ack_ok:
            return False, ack_msg

        # Register a completion waiter. _broadcast_macro_complete will .set()
        # the Event when bambu_mqtt fires on_macro_complete.
        event = asyncio.Event()
        result: dict = {"status": "pending", "message": ""}
        self._macro_waiters[printer_id] = (event, result)

        try:
            while not event.is_set():
                if not client.state.connected:
                    logger.warning(
                        "[MACRO-WAIT] Printer %s disconnected while waiting for macro '%s'",
                        printer_id,
                        macro_name,
                    )
                    return False, "Printer disconnected during macro execution"
                await asyncio.sleep(0.5)
        finally:
            self._macro_waiters.pop(printer_id, None)

        return result["status"] == "completed", result.get("message", "")

    def stop_print(self, printer_id: int) -> bool:
        """Stop the current print on a connected printer."""
        if printer_id in self._clients:
            return self._clients[printer_id].stop_print()
        return False

    async def wait_for_cooldown(
        self,
        printer_id: int,
        target_temp: float = 50.0,
        timeout: int = 600,
        check_interval: int = 10,
    ) -> bool:
        """Wait for the nozzle to cool down to a safe temperature.

        Args:
            printer_id: The printer to monitor
            target_temp: Target temperature to wait for (default 50°C)
            timeout: Maximum seconds to wait (default 600s = 10 min)
            check_interval: Seconds between temperature checks (default 10s)

        Returns:
            True if cooled down, False if timeout or not connected
        """
        import logging

        logger = logging.getLogger(__name__)

        elapsed = 0
        while elapsed < timeout:
            state = self.get_status(printer_id)
            if not state or not state.connected:
                logger.warning("Printer %s disconnected during cooldown wait", printer_id)
                return False

            # Check nozzle temperature (and nozzle_2 for dual extruders)
            nozzle_temp = state.temperatures.get("nozzle", 0)
            nozzle_2_temp = state.temperatures.get("nozzle_2", 0)
            max_temp = max(nozzle_temp, nozzle_2_temp)

            if max_temp <= target_temp:
                logger.info("Printer %s cooled down to %s°C", printer_id, max_temp)
                return True

            logger.debug("Printer %s nozzle at %s°C, waiting for %s°C...", printer_id, max_temp, target_temp)
            await asyncio.sleep(check_interval)
            elapsed += check_interval

        logger.warning("Printer %s cooldown timeout after %ss", printer_id, timeout)
        return False

    def enable_logging(self, printer_id: int, enabled: bool = True) -> bool:
        """Enable or disable MQTT logging for a printer."""
        if printer_id in self._clients:
            self._clients[printer_id].enable_logging(enabled)
            return True
        return False

    def get_logs(self, printer_id: int) -> list[MQTTLogEntry]:
        """Get MQTT logs for a printer."""
        if printer_id in self._clients:
            return self._clients[printer_id].get_logs()
        return []

    def clear_logs(self, printer_id: int) -> bool:
        """Clear MQTT logs for a printer."""
        if printer_id in self._clients:
            self._clients[printer_id].clear_logs()
            return True
        return False

    def is_logging_enabled(self, printer_id: int) -> bool:
        """Check if logging is enabled for a printer."""
        if printer_id in self._clients:
            return self._clients[printer_id].logging_enabled
        return False

    def send_drying_command(
        self,
        printer_id: int,
        ams_id: int,
        temp: int,
        duration: int,
        mode: int = 1,
        filament: str = "",
        rotate_tray: bool = False,
    ) -> bool:
        """Send AMS drying command to printer."""
        if printer_id not in self._clients:
            return False
        return self._clients[printer_id].send_drying_command(ams_id, temp, duration, mode, filament, rotate_tray)

    def request_status_update(self, printer_id: int) -> bool:
        """Request a full status update from the printer.

        This sends a 'pushall' command to get the latest data including nozzle info.
        """
        if printer_id in self._clients:
            return self._clients[printer_id].request_status_update()
        return False

    async def test_connection(
        self,
        ip_address: str,
        serial_number: str,
        access_code: str,
    ) -> dict:
        """Test connection to a printer without persisting."""
        client = BambuMQTTClient(
            ip_address=ip_address,
            serial_number=serial_number,
            access_code=access_code,
        )

        try:
            client.connect()
            await asyncio.sleep(2)

            result = {
                "success": client.state.connected,
                "state": client.state.state if client.state.connected else None,
                "model": client.state.raw_data.get("device_model"),
            }
        finally:
            client.disconnect()

        return result

    async def _broadcast_macro_complete(self, printer_id: int, macro_name: str, status: str):
        """Notify waiting dispatch pipeline, then broadcast via WebSocket."""
        # Unblock the dispatch pipeline first — it's blocking on the Event.
        waiter = self._macro_waiters.get(printer_id)
        if waiter:
            event, result = waiter
            result["status"] = status
            result["message"] = f"Macro '{macro_name}' {status}"
            event.set()

        from backend.app.core.websocket import ws_manager

        printer_name = self._printer_info.get(printer_id)
        await ws_manager.broadcast(
            {
                "type": "macro_executed",
                "data": {
                    "printer_id": printer_id,
                    "printer_name": printer_name.name if printer_name else str(printer_id),
                    "macro_name": macro_name,
                    "status": status,
                    "success": status == "completed",
                    "message": f"Macro '{macro_name}' {status}",
                },
            }
        )


def get_derived_status_name(state: PrinterState, model: str | None = None) -> str | None:
    """
    Compute a human-readable status name based on printer state.

    Uses stg_cur when available, otherwise derives status from temperature data
    when the printer is heating before a print starts.

    Args:
        state: The printer state to analyze
        model: Optional printer model for model-specific workarounds
    """
    # Macro executing - show macro name instead of default "Printing" text
    if state.macro_executing and state.stg_cur == 0:
        return f"Executing: {state.macro_executing}"

    # A1/A1 Mini firmware bug: some versions report stg_cur=0 when idle
    # Only correct this specific case (IDLE + stg_cur=0) for affected models
    if state.state == "IDLE" and state.stg_cur == 0 and has_stg_cur_idle_bug(model):
        return None

    # If we have a valid calibration stage, use it
    # X1 models use -1 for idle, A1/P1 models use 255 for idle
    # Valid stage numbers are 0-254
    if 0 <= state.stg_cur < 255:
        return get_stage_name(state.stg_cur)

    # If not in RUNNING state, no derived status needed
    if state.state != "RUNNING":
        return None

    # Check if we're in an early phase where temperatures are heating
    temps = state.temperatures or {}
    progress = state.progress or 0

    # Only derive heating status when progress is very low (< 2%)
    # This indicates we're in the preparation phase, not actually printing
    if progress >= 2:
        return None

    # Check bed temperature - if target is set and current is significantly below
    bed_temp = temps.get("bed", 0)
    bed_target = temps.get("bed_target", 0)

    # Check nozzle temperature
    nozzle_temp = temps.get("nozzle", 0)
    nozzle_target = temps.get("nozzle_target", 0)

    # Temperature thresholds: consider "heating" if more than 10°C below target
    TEMP_THRESHOLD = 10

    # Determine what's heating (prioritize bed since it takes longer)
    if bed_target > 30 and (bed_target - bed_temp) > TEMP_THRESHOLD:
        return "Heating heatbed"
    elif nozzle_target > 30 and (nozzle_target - nozzle_temp) > TEMP_THRESHOLD:
        return "Heating nozzle"

    # If targets are set but we're close to them, we might be in final prep
    if bed_target > 30 or nozzle_target > 30:
        if progress == 0 and state.layer_num == 0:
            return "Preparing"

    return None


def printer_state_to_dict(state: PrinterState, printer_id: int | None = None, model: str | None = None) -> dict:
    """Convert PrinterState to a JSON-serializable dict.

    Args:
        state: The printer state to convert
        printer_id: Optional printer ID for generating cover URLs
        model: Optional printer model for filtering unsupported features
    """
    # Parse AMS data from raw_data
    ams_units = []
    vt_tray = []
    raw_data = state.raw_data or {}

    # Build K-profile lookup map: cali_idx -> k_value
    kprofile_map: dict[int, float] = {}
    for kp in state.kprofiles or []:
        if kp.slot_id is not None and kp.k_value:
            try:
                kprofile_map[kp.slot_id] = float(kp.k_value)
            except (ValueError, TypeError):
                pass  # Skip K-profile entries with unparseable values

    if "ams" in raw_data and isinstance(raw_data["ams"], list):
        for ams_data in raw_data["ams"]:
            trays = []
            for tray in ams_data.get("tray", []):
                tag_uid = tray.get("tag_uid")
                if tag_uid in ("", "0000000000000000"):
                    tag_uid = None
                tray_uuid = tray.get("tray_uuid")
                if tray_uuid in ("", "00000000000000000000000000000000"):
                    tray_uuid = None

                # Get K value: first try tray's k field, then lookup from K-profiles
                k_value = tray.get("k")
                cali_idx = tray.get("cali_idx")
                if k_value is None and cali_idx is not None and cali_idx in kprofile_map:
                    k_value = kprofile_map[cali_idx]

                trays.append(
                    {
                        "id": int(tray.get("id", 0)),
                        "tray_color": tray.get("tray_color"),
                        "tray_type": tray.get("tray_type"),
                        "tray_sub_brands": tray.get("tray_sub_brands"),
                        "tray_id_name": tray.get("tray_id_name"),
                        "tray_info_idx": tray.get("tray_info_idx"),
                        "remain": tray.get("remain", 0),
                        "k": k_value,
                        "cali_idx": cali_idx,
                        "tag_uid": tag_uid,
                        "tray_uuid": tray_uuid,
                        "nozzle_temp_min": tray.get("nozzle_temp_min"),
                        "nozzle_temp_max": tray.get("nozzle_temp_max"),
                        "drying_temp": tray.get("drying_temp"),
                        "drying_time": tray.get("drying_time"),
                        "state": tray.get("state"),
                    }
                )
            # Prefer humidity_raw (actual percentage) over humidity (index 1-5)
            humidity_raw = ams_data.get("humidity_raw")
            humidity_idx = ams_data.get("humidity")
            humidity_value = None

            if humidity_raw is not None:
                try:
                    humidity_value = int(humidity_raw)
                except (ValueError, TypeError):
                    pass  # Skip unparseable humidity; will try index fallback
            # Fall back to index if no raw value (index is 1-5, not percentage)
            if humidity_value is None and humidity_idx is not None:
                try:
                    humidity_value = int(humidity_idx)
                except (ValueError, TypeError):
                    pass  # Skip unparseable humidity index; humidity remains None

            # AMS-HT has 1 tray, regular AMS has 4 trays
            is_ams_ht = len(trays) == 1

            ams_units.append(
                {
                    "id": int(ams_data.get("id", 0)),
                    "humidity": humidity_value,
                    "temp": ams_data.get("temp"),
                    "is_ams_ht": is_ams_ht,
                    "tray": trays,
                    # Serial number: Bambu MQTT uses "sn" key on AMS unit objects
                    "serial_number": str(ams_data.get("sn") or ams_data.get("serial_number") or ""),
                    # Firmware version: populated by _handle_version_info from get_version
                    "sw_ver": str(ams_data.get("sw_ver") or ""),
                    # Drying: dry_time > 0 means drying is active (minutes remaining)
                    "dry_time": int(ams_data.get("dry_time") or 0),
                    # Drying status from info hex bits (0=Off, 1=Checking, 2=Drying, 3=Cooling, etc.)
                    "dry_status": int(ams_data.get("dry_status") or 0),
                    "dry_sub_status": int(ams_data.get("dry_sub_status") or 0),
                    # Cannot-dry reasons from firmware (e.g. 1=InsufficientPower, 8=NeedPluginPower)
                    "dry_sf_reason": list(ams_data.get("dry_sf_reason") or []),
                    # Module type: "ams", "n3f", "n3s" (from get_version)
                    "module_type": str(ams_data.get("module_type") or ""),
                }
            )

    # Parse virtual tray (external spool) - now a list
    if "vt_tray" in raw_data:
        vt_tray_raw = raw_data["vt_tray"]
        if isinstance(vt_tray_raw, dict):
            vt_tray_raw = [vt_tray_raw]
        elif not isinstance(vt_tray_raw, list):
            vt_tray_raw = []
        for vt_data in vt_tray_raw:
            vt_tag_uid = vt_data.get("tag_uid")
            if vt_tag_uid in ("", "0000000000000000"):
                vt_tag_uid = None
            vt_tray_uuid = vt_data.get("tray_uuid")
            if vt_tray_uuid in ("", "00000000000000000000000000000000"):
                vt_tray_uuid = None

            # Get K value for vt_tray
            vt_k_value = vt_data.get("k")
            vt_cali_idx = vt_data.get("cali_idx")
            if vt_k_value is None and vt_cali_idx is not None and vt_cali_idx in kprofile_map:
                vt_k_value = kprofile_map[vt_cali_idx]

            tray_id = int(vt_data.get("id", 254))
            vt_tray.append(
                {
                    "id": tray_id,
                    "tray_color": vt_data.get("tray_color"),
                    "tray_type": vt_data.get("tray_type"),
                    "tray_sub_brands": vt_data.get("tray_sub_brands"),
                    "tray_id_name": vt_data.get("tray_id_name"),
                    "tray_info_idx": vt_data.get("tray_info_idx"),
                    "remain": vt_data.get("remain", 0),
                    "k": vt_k_value,
                    "cali_idx": vt_cali_idx,
                    "tag_uid": vt_tag_uid,
                    "tray_uuid": vt_tray_uuid,
                    "nozzle_temp_min": vt_data.get("nozzle_temp_min"),
                    "nozzle_temp_max": vt_data.get("nozzle_temp_max"),
                }
            )

    # Get ams_extruder_map from raw_data (populated by MQTT handler from AMS info field)
    ams_extruder_map = raw_data.get("ams_extruder_map", {})

    # Filter out chamber temp for models that don't have a real sensor
    # P1P, P1S, A1, A1Mini report meaningless chamber_temper values
    temperatures = state.temperatures
    if not supports_chamber_temp(model):
        temperatures = {
            k: v for k, v in temperatures.items() if k not in ("chamber", "chamber_target", "chamber_heating")
        }

    result = {
        "connected": state.connected,
        "state": state.state,
        "current_print": state.current_print,
        "subtask_name": state.subtask_name,
        "gcode_file": state.gcode_file,
        "progress": state.progress,
        "remaining_time": state.remaining_time,
        "layer_num": state.layer_num,
        "total_layers": state.total_layers,
        "temperatures": temperatures,
        "hms_errors": [
            {"code": e.code, "attr": e.attr, "module": e.module, "severity": e.severity}
            for e in (state.hms_errors or [])
        ],
        # AMS data for filament colors
        "ams": ams_units if ams_units else None,
        "vt_tray": vt_tray,
        # AMS status for filament change tracking
        "ams_status_main": state.ams_status_main,
        "ams_status_sub": state.ams_status_sub,
        "tray_now": state.tray_now,
        # Per-AMS extruder map: {ams_id: extruder_id} where 0=right, 1=left
        "ams_extruder_map": ams_extruder_map,
        # WiFi signal strength
        "wifi_signal": state.wifi_signal,
        "wired_network": state.wired_network,
        # Calibration stage tracking
        "stg_cur": state.stg_cur,
        "stg_cur_name": get_derived_status_name(state, model),
        # Printable objects count for skip objects feature
        "printable_objects_count": len(state.printable_objects),
        # Fan speeds (0-100 percentage, None if not available)
        "cooling_fan_speed": state.cooling_fan_speed,
        "big_fan1_speed": state.big_fan1_speed,
        "big_fan2_speed": state.big_fan2_speed,
        "heatbreak_fan_speed": state.heatbreak_fan_speed,
        # Chamber light state
        "chamber_light": state.chamber_light,
        # Active extruder for dual-nozzle printers (0=right, 1=left)
        "active_extruder": state.active_extruder,
        # H2C nozzle rack (tool-changer dock positions)
        # Map raw MQTT field names (type/diameter) to schema names (nozzle_type/nozzle_diameter)
        "nozzle_rack": [
            {
                "id": n.get("id", 0),
                "nozzle_type": n.get("type", ""),
                "nozzle_diameter": n.get("diameter", ""),
                "wear": n.get("wear"),
                "stat": n.get("stat"),
                "max_temp": n.get("max_temp", 0),
                "serial_number": n.get("serial_number", ""),
                "filament_color": n.get("filament_color", ""),
                "filament_id": n.get("filament_id", ""),
            }
            for n in (state.nozzle_rack or [])
        ],
        # AMS drying support
        "supports_drying": supports_drying(model, state.firmware_version),
    }
    # Add cover URL if there's an active print and printer_id is provided
    # Include PAUSE state so skip objects modal can show cover
    if printer_id and state.state in ("RUNNING", "PAUSE") and state.gcode_file:
        result["cover_url"] = f"/api/v1/printers/{printer_id}/cover"
    else:
        result["cover_url"] = None
    return result


# Global printer manager instance
printer_manager = PrinterManager()


async def init_printer_connections(db: AsyncSession):
    """Initialize connections to all active printers."""
    result = await db.execute(select(Printer).where(Printer.is_active.is_(True)))
    printers = result.scalars().all()

    for printer in printers:
        await printer_manager.connect_printer(printer)
