"""MQTT Relay Service for publishing BamDude events to external MQTT brokers.

This service enables integration with external automation systems like
Node-RED, Home Assistant, and other MQTT-based platforms.
"""

import asyncio
import json
import logging
import ssl
import threading
import time
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt

from backend.app.utils.paho_teardown import retire_paho_client

logger = logging.getLogger(__name__)


# Fields the printer status topic takes from the same dict the REST status and the
# WebSocket send, so a subscriber reads them in the shape it already knows (#3401).
_STATUS_FIELDS_FROM_STATE = ("door_open", "active_extruder", "tray_now", "ams", "vt_tray", "hms_errors")


def configure_broker_tls(client: mqtt.Client, *, verify: bool) -> None:
    """TLS for a connection to the operator's own broker (the relay and the MQTT smart plugs).

    Unverified by default: a local Mosquitto with a self-signed certificate is the ordinary home
    setup, and it must keep working. With ``verify`` (Settings -> "Verify the broker's certificate")
    the chain and the host name are checked against the system's trusted authorities, and a broker
    that fails the check is not connected to.
    """
    if verify:
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        client.tls_insecure_set(False)
    else:
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)


class MQTTRelayService:
    """Publishes BamDude events to an external MQTT broker."""

    # Minimum interval between status updates per printer (seconds)
    STATUS_THROTTLE_SECONDS = 1.0

    def __init__(self):
        self.client: mqtt.Client | None = None
        self.enabled = False
        self.connected = False
        self.topic_prefix = "bamdude"
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._broker = ""
        self._port = 1883
        self._last_printer_status: dict[int, float] = {}  # printer_id -> last publish timestamp
        self._last_printer_connected: dict[int, bool] = {}  # printer_id -> `connected` in the last publish
        self._last_status_payload: dict[int, dict] = {}
        self._smart_plug_service = None  # Lazy import to avoid circular dependency
        self._settings: dict = {}  # Store settings for smart plug service
        self._disconnection_event: threading.Event | None = None

    async def configure(self, settings: dict) -> bool:
        """Configure MQTT connection from settings.

        Returns True if connection was successful or MQTT is disabled.
        """
        self.enabled = settings.get("mqtt_enabled", False)
        self._settings = settings  # Store for smart plug service

        if not self.enabled:
            await self.disconnect()
            # Also configure smart plug service (will disable it)
            await self._configure_smart_plug_service(settings)
            logger.info("MQTT relay disabled")
            return True

        broker = settings.get("mqtt_broker", "")
        port = settings.get("mqtt_port", 1883)
        username = settings.get("mqtt_username", "")
        password = settings.get("mqtt_password", "")
        self.topic_prefix = settings.get("mqtt_topic_prefix", "bamdude")
        use_tls = settings.get("mqtt_use_tls", False)
        self._tls_verify = bool(settings.get("mqtt_tls_verify", False))

        if not broker:
            logger.warning("MQTT enabled but no broker configured")
            return False

        # Store for status endpoint
        self._broker = broker
        self._port = port

        # Disconnect existing connection if settings changed
        if self.client:
            await self.disconnect()

        # Create and connect client
        result = await self._connect(broker, port, username, password, use_tls)

        # Configure smart plug service with same settings
        await self._configure_smart_plug_service(settings)

        return result

    async def _configure_smart_plug_service(self, settings: dict):
        """Configure the MQTT smart plug service with the same broker settings."""
        try:
            if self._smart_plug_service is None:
                from backend.app.services.mqtt_smart_plug import mqtt_smart_plug_service

                self._smart_plug_service = mqtt_smart_plug_service

            await self._smart_plug_service.configure(settings)
        except Exception as e:
            logger.error("Failed to configure MQTT smart plug service: %s", e)

    @property
    def smart_plug_service(self):
        """Get the MQTT smart plug service instance."""
        if self._smart_plug_service is None:
            from backend.app.services.mqtt_smart_plug import mqtt_smart_plug_service

            self._smart_plug_service = mqtt_smart_plug_service
        return self._smart_plug_service

    async def _connect(self, broker: str, port: int, username: str, password: str, use_tls: bool) -> bool:
        """Establish MQTT connection."""
        try:
            # Create client with callback API version 2 (use MQTTv311 for broader compatibility)
            self.client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"bamdude-{id(self)}",
                protocol=mqtt.MQTTv311,
            )

            # Set up callbacks
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect

            # The broker publishes this for us when the connection is lost without a clean
            # disconnect (a crash, a cut cable): the retained service status must not stay "online".
            self.client.will_set(
                f"{self.topic_prefix}/status",
                json.dumps({"status": "offline", "timestamp": datetime.now(timezone.utc).isoformat()}),
                qos=1,
                retain=True,
            )

            # Configure authentication
            if username:
                self.client.username_pw_set(username, password)

            if use_tls:
                configure_broker_tls(self.client, verify=getattr(self, "_tls_verify", False))

            # Run connect_async in thread pool with timeout to avoid blocking
            # on unreachable brokers (connect_async does synchronous socket creation)
            try:
                await asyncio.wait_for(asyncio.to_thread(self.client.connect_async, broker, port, 60), timeout=3.0)
            except TimeoutError:
                logger.warning("MQTT relay connection to %s:%s timed out", broker, port)
                return False

            self.client.loop_start()

            # Wait briefly for connection callback
            await asyncio.sleep(1.0)

            if self.connected:
                logger.info("MQTT relay connected to %s:%s", broker, port)
                # Publish online status
                self._publish_status("online")
                return True
            else:
                logger.warning("MQTT relay connection pending to %s:%s", broker, port)
                return True  # Connection is async, may succeed later

        except Exception as e:
            logger.error("MQTT relay connection failed: %s", e)
            self.connected = False
            return False

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: dict,
        reason_code: int | mqtt.ReasonCode,
        properties: mqtt.Properties | None = None,
    ):
        """Callback when connected to broker."""
        # Handle both MQTTv311 (int) and MQTTv5 (ReasonCode) return codes
        rc = reason_code if isinstance(reason_code, int) else reason_code.value
        if rc == 0:
            self.connected = True
            logger.info("MQTT relay connected successfully")
            # Publish online status
            self._publish_status("online")
            self.publish_plate_clear_snapshot()
        else:
            self.connected = False
            logger.error("MQTT relay connection failed: %s", reason_code)

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags_or_rc: dict | int | mqtt.ReasonCode,
        reason_code: int | mqtt.ReasonCode | None = None,
        properties: mqtt.Properties | None = None,
    ):
        """Callback when disconnected from broker."""
        self.connected = False
        # Handle both MQTTv311 (rc as 3rd param) and MQTTv5 (flags, rc, props)
        rc = reason_code if reason_code is not None else flags_or_rc
        rc_val = rc if isinstance(rc, int) else getattr(rc, "value", 0)
        if rc_val != 0:
            logger.warning("MQTT relay disconnected: %s", rc)
        else:
            logger.info("MQTT relay disconnected cleanly")
        if self._disconnection_event:
            self._disconnection_event.set()

    async def disconnect(self, timeout: float = 0):
        """Disconnect from MQTT broker."""
        if self.client:
            try:
                # Publish offline status before disconnecting
                self._publish_status("offline")
                self._disconnection_event = threading.Event()
                self.client.disconnect()
                await asyncio.to_thread(self._disconnection_event.wait, timeout=timeout)
                # Not loop_stop(): its join has no bound, and a wedged broker
                # would keep the process from exiting (upstream #3068).
                retire_paho_client(self.client, "relay")
            except Exception as e:
                logger.debug("MQTT disconnect error (ignored): %s", e)
            finally:
                self.client = None
                self.connected = False

    def _publish_status(self, status: str):
        """Publish BamDude status (online/offline)."""
        self._publish(
            f"{self.topic_prefix}/status",
            {"status": status, "timestamp": datetime.now(timezone.utc).isoformat()},
            retain=True,
        )

    def _publish(self, topic: str, payload: dict, retain: bool = False):
        """Publish message to MQTT broker."""
        if not self.client or not self.connected:
            return

        try:
            with self._lock:
                self.client.publish(topic, json.dumps(payload, default=str), qos=1, retain=retain)
        except Exception as e:
            logger.debug("MQTT publish error: %s", e)

    def get_status(self) -> dict:
        """Get current MQTT relay status for API."""
        return {
            "enabled": self.enabled,
            "connected": self.connected,
            "broker": self._broker if self.enabled else "",
            "port": self._port if self.enabled else 0,
            "topic_prefix": self.topic_prefix,
        }

    # =========================================================================
    # Printer Events
    # =========================================================================

    async def on_printer_status(
        self,
        printer_id: int,
        state: Any,
        printer_name: str,
        printer_serial: str,
        awaiting_plate_clear: bool = False,
        force: bool = False,
    ):
        """Publish printer status change (throttled to 1 update/sec per printer).

        A change of ``connected`` is never throttled: the update that says a printer
        dropped is retained, and held back it would leave the previous status
        standing for as long as the printer stays quiet (#3401).
        """
        if not self.enabled or not self.connected:
            return

        # Throttle status updates to avoid flooding MQTT broker
        now = time.time()
        last_publish = self._last_printer_status.get(printer_id, 0)
        last_connected = self._last_printer_connected.get(printer_id)
        connected_changed = last_connected is not None and last_connected != state.connected
        if not force and not connected_changed and now - last_publish < self.STATUS_THROTTLE_SECONDS:
            return  # Skip this update, too soon since last publish
        self._last_printer_status[printer_id] = now
        self._last_printer_connected[printer_id] = state.connected

        from backend.app.services.printer_manager import printer_state_to_dict

        snapshot = printer_state_to_dict(state, printer_id)

        # Build status payload from PrinterState
        payload = {
            "printer_id": printer_id,
            "printer_name": printer_name,
            "printer_serial": printer_serial,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "connected": state.connected,
            "state": state.state,
            "progress": state.progress,
            "remaining_time": state.remaining_time,
            "layer_num": state.layer_num,
            "total_layers": state.total_layers,
            "current_print": state.current_print,
            "subtask_name": state.subtask_name,
            "gcode_file": state.gcode_file,
            "temperatures": state.temperatures,
            "wifi_signal": state.wifi_signal,
            "chamber_light": state.chamber_light,
            "speed_level": state.speed_level,
            "cooling_fan_speed": state.cooling_fan_speed,
            "big_fan1_speed": state.big_fan1_speed,
            "big_fan2_speed": state.big_fan2_speed,
            "heatbreak_fan_speed": state.heatbreak_fan_speed,
            "left_aux_fan_speed": (getattr(state, "airduct_parts", {}) or {}).get(10, {}).get("state"),
            "exhaust_fan_present": (3 in state.airduct_parts) if getattr(state, "airduct_parts", None) else None,
            **{field: snapshot[field] for field in _STATUS_FIELDS_FROM_STATE},
            "awaiting_plate_clear": awaiting_plate_clear,
        }

        self._last_status_payload[printer_id] = payload

        self._publish(
            f"{self.topic_prefix}/printers/{printer_serial}/status",
            payload,
            retain=True,
        )

    async def on_plate_clear_state(self, printer_id: int, printer_name: str, printer_serial: str, awaiting: bool):
        if not self.enabled or not self.connected:
            return
        self._publish(
            f"{self.topic_prefix}/printers/{printer_serial}/plate_clear",
            {
                "printer_id": printer_id,
                "printer_name": printer_name,
                "printer_serial": printer_serial,
                "awaiting": awaiting,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            retain=True,
        )
        status = self._last_status_payload.get(printer_id)
        if status is not None:
            status = {**status, "awaiting_plate_clear": awaiting, "timestamp": datetime.now(timezone.utc).isoformat()}
            self._last_status_payload[printer_id] = status
            self._publish(f"{self.topic_prefix}/printers/{printer_serial}/status", status, retain=True)

    def publish_plate_clear_snapshot(self):
        """Republish retained gate states after startup/reconnect, including offline printers."""
        if not self.enabled or not self.connected:
            return
        from backend.app.services.printer_manager import printer_manager

        for printer_id, info in list(printer_manager._printer_info.items()):
            awaiting = printer_manager.is_awaiting_plate_clear(printer_id)
            self._publish(
                f"{self.topic_prefix}/printers/{info.serial_number}/plate_clear",
                {
                    "printer_id": printer_id,
                    "printer_name": info.name,
                    "printer_serial": info.serial_number,
                    "awaiting": awaiting,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                retain=True,
            )

    async def on_printer_online(self, printer_id: int, printer_name: str, printer_serial: str):
        """Publish printer came online event."""
        if not self.enabled or not self.connected:
            return

        self._publish(
            f"{self.topic_prefix}/printers/{printer_serial}/online",
            {
                "printer_id": printer_id,
                "printer_name": printer_name,
                "printer_serial": printer_serial,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def on_printer_offline(self, printer_id: int, printer_name: str, printer_serial: str):
        """Publish printer went offline event."""
        if not self.enabled or not self.connected:
            return

        self._publish(
            f"{self.topic_prefix}/printers/{printer_serial}/offline",
            {
                "printer_id": printer_id,
                "printer_name": printer_name,
                "printer_serial": printer_serial,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def on_print_start(
        self,
        printer_id: int,
        printer_name: str,
        printer_serial: str,
        filename: str,
        subtask_name: str,
    ):
        """Publish print started event."""
        if not self.enabled or not self.connected:
            return

        self._publish(
            f"{self.topic_prefix}/printers/{printer_serial}/print/started",
            {
                "printer_id": printer_id,
                "printer_name": printer_name,
                "printer_serial": printer_serial,
                "filename": filename,
                "subtask_name": subtask_name,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def on_print_complete(
        self,
        printer_id: int,
        printer_name: str,
        printer_serial: str,
        filename: str,
        subtask_name: str,
        status: str,
    ):
        """Publish print completed event."""
        if not self.enabled or not self.connected:
            return

        # Determine topic based on status
        if status == "completed":
            topic = f"{self.topic_prefix}/printers/{printer_serial}/print/completed"
        else:
            topic = f"{self.topic_prefix}/printers/{printer_serial}/print/failed"

        self._publish(
            topic,
            {
                "printer_id": printer_id,
                "printer_name": printer_name,
                "printer_serial": printer_serial,
                "filename": filename,
                "subtask_name": subtask_name,
                "status": status,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def on_ams_change(
        self,
        printer_id: int,
        printer_name: str,
        printer_serial: str,
        ams_data: list,
    ):
        """Publish AMS filament change event."""
        if not self.enabled or not self.connected:
            return

        self._publish(
            f"{self.topic_prefix}/printers/{printer_serial}/ams/changed",
            {
                "printer_id": printer_id,
                "printer_name": printer_name,
                "printer_serial": printer_serial,
                "ams_units": ams_data,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def on_printer_error(
        self,
        printer_id: int,
        printer_name: str,
        printer_serial: str,
        errors: list,
    ):
        """Publish printer HMS error event."""
        if not self.enabled or not self.connected:
            return

        self._publish(
            f"{self.topic_prefix}/printers/{printer_serial}/error",
            {
                "printer_id": printer_id,
                "printer_name": printer_name,
                "printer_serial": printer_serial,
                "errors": errors,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    # =========================================================================
    # Print Queue Events
    # =========================================================================

    async def on_queue_job_added(
        self,
        job_id: int,
        filename: str,
        printer_id: int | None,
        printer_name: str | None,
    ):
        """Publish job added to queue event."""
        if not self.enabled or not self.connected:
            return

        self._publish(
            f"{self.topic_prefix}/queue/job_added",
            {
                "job_id": job_id,
                "filename": filename,
                "printer_id": printer_id,
                "printer_name": printer_name,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def on_queue_jobs_added(self, item_ids: list[int]) -> None:
        """Publish ``queue/job_added`` for committed printer-queue rows (see ``queue_job_relay``).

        Each row is read here, after its commit: the file name is the archive's, else the library
        file's, else -- a job whose original rows are gone, first-class since m173 -- the name its own
        captured snapshot was queued under (G03 D7.3). A row that is gone again by now is skipped.

        A calibration print is the wizard's own job, not work somebody queued: it is not announced
        (G03 D7.3 -- skipped rather than marked, the smaller change to a published payload). Never raises.
        """
        if not item_ids or not self.enabled or not self.connected:
            return
        try:
            from contextlib import suppress

            from sqlalchemy import select

            from backend.app.core.database import async_session
            from backend.app.models.archive import PrintArchive
            from backend.app.models.library import LibraryFile
            from backend.app.models.print_queue import PrintQueueItem
            from backend.app.models.printer import Printer
            from backend.app.models.queue_source import QueueSource
            from backend.app.services.queue_source_descriptor import stored_descriptor
            from backend.app.services.source_io import SourceUnavailable

            async with async_session() as db:
                rows = (
                    await db.execute(
                        select(
                            PrintQueueItem.id,
                            PrintQueueItem.queue_id,
                            PrintArchive.filename,
                            LibraryFile.filename,
                            Printer.name,
                            QueueSource,
                            PrintQueueItem.source_snapshot,
                        )
                        .outerjoin(PrintArchive, PrintArchive.id == PrintQueueItem.archive_id)
                        .outerjoin(LibraryFile, LibraryFile.id == PrintQueueItem.library_file_id)
                        .outerjoin(Printer, Printer.id == PrintQueueItem.queue_id)
                        .outerjoin(QueueSource, QueueSource.id == PrintQueueItem.queue_source_id)
                        .where(PrintQueueItem.id.in_(item_ids), PrintQueueItem.is_calibration.is_(False))
                        .order_by(PrintQueueItem.id)
                    )
                ).all()
            for job_id, printer_id, archive_name, library_name, printer_name, source, snapshot in rows:
                filename = archive_name or library_name or ""
                if not filename and source is not None:
                    from backend.app.services.filament_intake import source_display_filename

                    # ``source_display_filename`` refuses the object's hash: an unreadable snapshot names nothing.
                    with suppress(SourceUnavailable):
                        filename = source_display_filename(stored_descriptor(source, snapshot))
                await self.on_queue_job_added(
                    job_id=job_id,
                    filename=filename,
                    printer_id=printer_id,
                    printer_name=printer_name,
                )
        except Exception as e:  # noqa: BLE001 -- the jobs are queued; announcing never undoes that
            logger.debug("MQTT relay: queued jobs not published: %s", e)

    async def on_queue_job_started(
        self,
        job_id: int,
        filename: str,
        printer_id: int,
        printer_name: str,
        printer_serial: str,
    ):
        """Publish queued job started printing event."""
        if not self.enabled or not self.connected:
            return

        self._publish(
            f"{self.topic_prefix}/queue/job_started",
            {
                "job_id": job_id,
                "filename": filename,
                "printer_id": printer_id,
                "printer_name": printer_name,
                "printer_serial": printer_serial,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def on_queue_job_completed(
        self,
        job_id: int,
        filename: str,
        printer_id: int,
        printer_name: str,
        status: str,
    ):
        """Publish queued job finished event."""
        if not self.enabled or not self.connected:
            return

        topic = (
            f"{self.topic_prefix}/queue/job_completed"
            if status == "completed"
            else f"{self.topic_prefix}/queue/job_failed"
        )

        self._publish(
            topic,
            {
                "job_id": job_id,
                "filename": filename,
                "printer_id": printer_id,
                "printer_name": printer_name,
                "status": status,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    # =========================================================================
    # Maintenance Events
    # =========================================================================

    async def on_maintenance_alert(
        self,
        printer_id: int,
        printer_name: str,
        maintenance_type: str,
        current_value: float | None,
        threshold: float | None,
        unit: str = "hours",
    ):
        """Publish maintenance alert triggered event.

        ``current_value`` is what has passed since the item was last done and ``threshold`` its
        interval, both in ``unit`` ("hours" of printing or calendar "days").
        """
        if not self.enabled or not self.connected:
            return

        self._publish(
            f"{self.topic_prefix}/maintenance/alert",
            {
                "printer_id": printer_id,
                "printer_name": printer_name,
                "maintenance_type": maintenance_type,
                "current_value": current_value,
                "threshold": threshold,
                "unit": unit,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def on_maintenance_items_due(self, printer_id: int, printer_name: str, items) -> None:
        """One ``maintenance/alert`` per item of a maintenance overview that is due or close to it.

        Every place that sends the maintenance-due notification calls this with the same items, so
        the alert carries the figures the notification was decided on. Never raises.
        """
        if not self.enabled or not self.connected:
            return
        for item in items:
            try:
                in_days = (getattr(item, "interval_type", None) or "hours") == "days"
                await self.on_maintenance_alert(
                    printer_id=printer_id,
                    printer_name=printer_name,
                    maintenance_type=item.maintenance_type_name,
                    current_value=item.days_since_maintenance if in_days else item.hours_since_maintenance,
                    threshold=item.interval_hours,
                    unit="days" if in_days else "hours",
                )
            except Exception as e:  # noqa: BLE001 -- a relay failure never costs the caller its work
                logger.debug("MQTT relay: maintenance alert not published: %s", e)

    async def on_maintenance_reset(
        self,
        printer_id: int,
        printer_name: str,
        maintenance_type: str,
    ):
        """Publish maintenance counter reset event."""
        if not self.enabled or not self.connected:
            return

        self._publish(
            f"{self.topic_prefix}/maintenance/reset",
            {
                "printer_id": printer_id,
                "printer_name": printer_name,
                "maintenance_type": maintenance_type,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    # =========================================================================
    # Archive Events
    # =========================================================================

    async def on_archive_created(
        self,
        archive_id: int,
        print_name: str,
        printer_name: str,
        status: str,
    ):
        """Publish print archived event."""
        if not self.enabled or not self.connected:
            return

        self._publish(
            f"{self.topic_prefix}/archive/created",
            {
                "archive_id": archive_id,
                "print_name": print_name,
                "printer_name": printer_name,
                "status": status,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def on_archive_updated(
        self,
        archive_id: int,
        print_name: str,
        status: str,
    ):
        """Publish archive record updated event."""
        if not self.enabled or not self.connected:
            return

        self._publish(
            f"{self.topic_prefix}/archive/updated",
            {
                "archive_id": archive_id,
                "print_name": print_name,
                "status": status,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def on_reconciled_prints(self, printer_id: int, archive_ids: list[int]) -> None:
        """Publish the end of prints the start-up sweep closed (they ended while BamDude was down).

        The live completion handler never ran for them, so without this a subscriber saw the print
        start and never end. Same topics and fields as the live path. Never raises.
        """
        if not archive_ids or not self.enabled or not self.connected:
            return
        try:
            from sqlalchemy import select

            from backend.app.core.database import async_session
            from backend.app.models.archive import PrintArchive
            from backend.app.models.print_queue import PrintQueueItem
            from backend.app.services.printer_manager import printer_manager

            async with async_session() as db:
                archives = (
                    await db.execute(
                        select(PrintArchive.id, PrintArchive.filename, PrintArchive.print_name, PrintArchive.status)
                        .where(PrintArchive.id.in_(archive_ids))
                        .order_by(PrintArchive.id)
                    )
                ).all()
                jobs = (
                    await db.execute(
                        select(PrintQueueItem.id, PrintQueueItem.archive_id, PrintQueueItem.status).where(
                            PrintQueueItem.archive_id.in_(archive_ids)
                        )
                    )
                ).all()
            info = printer_manager.get_printer(printer_id)
            printer_name = info.name if info else "Unknown"
            for archive_id, filename, print_name, status in archives:
                if status == "printing":
                    continue  # not closed after all
                name = print_name or filename or ""
                if info:
                    await self.on_print_complete(
                        printer_id, info.name, info.serial_number, filename or "", name, status
                    )
                await self.on_archive_updated(archive_id=archive_id, print_name=name, status=status)
                for job_id, job_archive_id, job_status in jobs:
                    if job_archive_id == archive_id and job_status != "printing":
                        await self.on_queue_job_completed(
                            job_id=job_id,
                            filename=name,
                            printer_id=printer_id,
                            printer_name=printer_name,
                            status=job_status,
                        )
        except Exception as e:  # noqa: BLE001 -- the connect path survives a relay failure
            logger.debug("MQTT relay: reconciled prints not published: %s", e)

    # =========================================================================
    # Filament/Spoolman Events
    # =========================================================================

    async def on_filament_low(
        self,
        printer_id: int,
        slot: int | str,
        remaining_percent: int,
        color: str | None = None,
        spool_id: int | None = None,
    ) -> None:
        """Publish ``printers/{serial}/filament/low`` when a loaded spool crosses its low mark.

        Called by the producers of the low-filament notification, once per crossing, whether or not
        any notification provider listens. A printer BamDude holds no record of is not published
        (no serial for the topic). Never raises.
        """
        if not self.enabled or not self.connected:
            return
        try:
            from backend.app.services.printer_manager import printer_manager

            info = printer_manager.get_printer(printer_id)
            if info is None:
                return
            self._publish(
                f"{self.topic_prefix}/printers/{info.serial_number}/filament/low",
                {
                    "printer_id": printer_id,
                    "printer_name": info.name,
                    "printer_serial": info.serial_number,
                    "slot": slot,
                    "remaining_percent": remaining_percent,
                    "color": color,
                    "spool_id": spool_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as e:  # noqa: BLE001 -- announcing never costs the caller its work
            logger.debug("MQTT relay: filament low not published: %s", e)

    # =========================================================================
    # Smart Plug Events
    # =========================================================================

    async def on_smart_plug_state(
        self,
        plug_id: int,
        plug_name: str,
        state: str,
        printer_id: int | None = None,
        printer_name: str | None = None,
    ):
        """Publish smart plug state change event."""
        if not self.enabled or not self.connected:
            return

        topic = f"{self.topic_prefix}/smart_plugs/on" if state == "on" else f"{self.topic_prefix}/smart_plugs/off"

        self._publish(
            topic,
            {
                "plug_id": plug_id,
                "plug_name": plug_name,
                "state": state,
                "printer_id": printer_id,
                "printer_name": printer_name,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def on_smart_plug_switched(self, plug_id: int, on: bool) -> None:
        """Publish ``smart_plugs/on|off`` for a plug BamDude has just switched. Never raises.

        The one call every place that switches a plug makes after the device accepted the command
        -- the button, the schedule, the automation around a print, the queue waking a printer,
        failure detection (``test_plug_switches_tell_the_relay.py`` fails a switch without it). The
        plug is named by id and read here: two of the callers hold only a stand-in object without a
        name. A Home Assistant script is not a plug and is not published.
        """
        if not self.enabled or not self.connected:
            return
        try:
            from sqlalchemy import select

            from backend.app.core.database import async_session
            from backend.app.models.printer import Printer
            from backend.app.models.smart_plug import SmartPlug

            async with async_session() as db:
                row = (
                    await db.execute(
                        select(
                            SmartPlug.name,
                            SmartPlug.plug_type,
                            SmartPlug.ha_entity_id,
                            SmartPlug.printer_id,
                            Printer.name,
                        )
                        .outerjoin(Printer, Printer.id == SmartPlug.printer_id)
                        .where(SmartPlug.id == plug_id)
                    )
                ).first()
            if row is None:
                return
            plug_name, plug_type, entity_id, printer_id, printer_name = row
            if plug_type == "homeassistant" and (entity_id or "").startswith("script."):
                return
            await self.on_smart_plug_state(
                plug_id=plug_id,
                plug_name=plug_name,
                state="on" if on else "off",
                printer_id=printer_id,
                printer_name=printer_name,
            )
        except Exception as e:  # noqa: BLE001 -- the switch already happened; announcing never undoes it
            logger.debug("MQTT relay: plug %s switch not published: %s", plug_id, e)

    async def on_smart_plug_energy(
        self,
        plug_id: int,
        plug_name: str,
        power: float | None,
        energy_today: float | None,
        energy_total: float | None,
    ):
        """Publish smart plug energy update event. A figure the plug does not report is ``null``."""
        if not self.enabled or not self.connected:
            return

        self._publish(
            f"{self.topic_prefix}/smart_plugs/energy",
            {
                "plug_id": plug_id,
                "plug_name": plug_name,
                "power_watts": power,
                "energy_today_kwh": energy_today,
                "energy_total_kwh": energy_total,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )


# Global instance
mqtt_relay = MQTTRelayService()

# Registers the session hooks that publish ``queue/job_added`` for every committed queue row.
from backend.app.services import queue_job_relay  # noqa: E402, F401
