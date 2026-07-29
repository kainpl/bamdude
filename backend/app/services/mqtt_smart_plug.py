"""MQTT Smart Plug Service for subscribing to external MQTT topics and extracting power/energy data.

This service enables integration with Shelly, Zigbee2MQTT, and other MQTT-based energy monitoring devices.
"""

import asyncio
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


@dataclass
class SmartPlugMQTTData:
    """Latest data received from an MQTT smart plug."""

    plug_id: int
    power: float | None = None  # Current power in watts
    energy: float | None = None  # Energy in kWh — whatever the operator's energy path points at
    energy_total: float | None = None  # Lifetime kWh — the only figure energy snapshots may use
    state: str | None = None  # "ON" or "OFF"
    # Timezone-AWARE, because ``is_reachable`` subtracts this from
    # ``datetime.now(timezone.utc)`` and ``_on_message`` writes an aware value.
    # A naive default made that subtraction raise TypeError for any plug that
    # had been subscribed but had not yet received a message — which is every
    # plug between being added and its device's first publish, and the status
    # endpoint calls ``is_reachable`` unconditionally.
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class MQTTDataSourceConfig:
    """Configuration for a single MQTT data source (power, energy, or state)."""

    topic: str
    path: str
    multiplier: float = 1.0  # For power/energy
    on_value: str | None = None  # For state (what value means "ON")


class MQTTSmartPlugService:
    """Subscribes to MQTT topics for smart plug energy monitoring."""

    # Consider plug unreachable if no message received in this time
    REACHABLE_TIMEOUT_MINUTES = 5

    # How long turn_on/turn_off wait for the device to report the new state.
    # MQTT gives no reply to a publish, so confirmation can only come from the
    # state topic coming back round. Worth waiting for: auto-off and Obico's
    # pause_and_off cut mains to a printer.
    CONTROL_CONFIRM_TIMEOUT_SECONDS: float = 3.0

    def __init__(self):
        self.client: mqtt.Client | None = None
        self.connected = False
        self._lock = threading.Lock()
        # topic -> list of (plug_id, data_type) where data_type is "power", "energy", or "state"
        self.subscriptions: dict[str, list[tuple[int, str]]] = {}
        # plug_id -> {data_type: MQTTDataSourceConfig}
        self.plug_configs: dict[int, dict[str, MQTTDataSourceConfig]] = {}
        # plug_id -> latest data
        self.plug_data: dict[int, SmartPlugMQTTData] = {}
        self._disconnection_event: threading.Event | None = None
        self._configured = False
        self._broker = ""
        self._port = 1883
        self._username = ""
        self._password = ""
        self._use_tls = False

    def is_configured(self) -> bool:
        """Check if the MQTT service is configured and connected."""
        return self._configured and self.connected

    def has_broker_settings(self) -> bool:
        """Check if broker settings are available (even if not connected yet)."""
        return bool(self._broker)

    async def configure(self, settings: dict) -> bool:
        """Configure MQTT connection from settings.

        Uses the same broker settings as the MQTT relay service.
        Returns True if connection was successful or MQTT is disabled.
        """
        enabled = settings.get("mqtt_enabled", False)

        if not enabled:
            await self.disconnect()
            self._configured = False
            logger.debug("MQTT smart plug service disabled (MQTT relay not enabled)")
            return True

        broker = settings.get("mqtt_broker", "")
        port = settings.get("mqtt_port", 1883)
        username = settings.get("mqtt_username", "")
        password = settings.get("mqtt_password", "")
        use_tls = settings.get("mqtt_use_tls", False)

        if not broker:
            logger.warning("MQTT smart plug service: no broker configured")
            self._configured = False
            return False

        # Check if settings changed
        settings_changed = (
            self._broker != broker
            or self._port != port
            or self._username != username
            or self._password != password
            or self._use_tls != use_tls
        )

        self._broker = broker
        self._port = port
        self._username = username
        self._password = password
        self._use_tls = use_tls
        self._configured = True

        # Disconnect and reconnect if settings changed
        if settings_changed and self.client:
            await self.disconnect()

        # Connect if not already connected
        if not self.client or not self.connected:
            return await self._connect()

        return True

    async def _connect(self) -> bool:
        """Establish MQTT connection."""
        import asyncio
        import ssl

        try:
            # Create client with callback API version 2
            self.client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"bamdude-smartplug-{id(self)}",
                protocol=mqtt.MQTTv311,
            )

            # Set up callbacks
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.on_message = self._on_message

            # Configure authentication
            if self._username:
                self.client.username_pw_set(self._username, self._password)

            # Configure TLS
            if self._use_tls:
                self.client.tls_set(cert_reqs=ssl.CERT_NONE)
                self.client.tls_insecure_set(True)

            # Connect with timeout
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self.client.connect_async, self._broker, self._port, 60),
                    timeout=3.0,
                )
            except TimeoutError:
                logger.warning("MQTT smart plug connection to %s:%s timed out", self._broker, self._port)
                return False

            self.client.loop_start()

            # Wait briefly for connection
            await asyncio.sleep(1.0)

            if self.connected:
                logger.info("MQTT smart plug service connected to %s:%s", self._broker, self._port)
                # Resubscribe to all topics
                self._resubscribe_all()
                return True
            else:
                logger.warning("MQTT smart plug connection pending to %s:%s", self._broker, self._port)
                return True  # Connection is async

        except Exception as e:
            logger.error("MQTT smart plug connection failed: %s", e)
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
        rc = reason_code if isinstance(reason_code, int) else reason_code.value
        if rc == 0:
            self.connected = True
            logger.info("MQTT smart plug service connected successfully")
            # Resubscribe to all topics
            self._resubscribe_all()
        else:
            self.connected = False
            logger.error("MQTT smart plug connection failed: %s", reason_code)

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
        rc = reason_code if reason_code is not None else flags_or_rc
        rc_val = rc if isinstance(rc, int) else getattr(rc, "value", 0)
        if rc_val != 0:
            logger.warning("MQTT smart plug service disconnected: %s", rc)
        else:
            logger.info("MQTT smart plug service disconnected cleanly")
        if self._disconnection_event:
            self._disconnection_event.set()

    def _on_message(self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage):
        """Handle incoming MQTT message, extract data using JSON path."""
        topic = msg.topic

        with self._lock:
            subscriptions = self.subscriptions.get(topic, [])
            if not subscriptions:
                return

            # Parse JSON payload (or treat as raw value)
            try:
                payload = json.loads(msg.payload.decode("utf-8"))
                is_json = True
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Not JSON - treat the whole payload as a raw value
                payload = msg.payload.decode("utf-8").strip()
                is_json = False

            # Process for each subscribed (plug_id, data_type)
            for plug_id, data_type in subscriptions:
                configs = self.plug_configs.get(plug_id, {})
                config = configs.get(data_type)
                if not config:
                    continue

                # Extract value using path (or use raw payload if no path)
                if is_json and config.path:
                    raw_value = self._extract_json_path(payload, config.path)
                elif is_json and not config.path:
                    # JSON but no path - if it's a simple value use it, otherwise skip
                    if isinstance(payload, (int, float, str, bool)):
                        raw_value = payload
                    else:
                        # Can't use a dict/list as a value
                        logger.debug("MQTT plug %s: JSON payload is object/array but no path configured", plug_id)
                        continue
                else:
                    # Raw value (non-JSON)
                    raw_value = payload

                if raw_value is None:
                    continue

                # Initialize plug data if needed
                if plug_id not in self.plug_data:
                    self.plug_data[plug_id] = SmartPlugMQTTData(plug_id=plug_id)

                data = self.plug_data[plug_id]
                data.last_seen = datetime.now(timezone.utc)

                # Process based on data type
                if data_type == "power":
                    try:
                        data.power = float(raw_value) * config.multiplier
                        logger.debug("MQTT smart plug %s: power=%s", plug_id, data.power)
                    except (ValueError, TypeError):
                        pass  # Ignore unparseable power reading from MQTT

                elif data_type == "energy":
                    try:
                        data.energy = float(raw_value) * config.multiplier
                        logger.debug("MQTT smart plug %s: energy=%s", plug_id, data.energy)
                    except (ValueError, TypeError):
                        pass  # Ignore unparseable energy reading from MQTT

                elif data_type == "energy_total":
                    try:
                        data.energy_total = float(raw_value) * config.multiplier
                        logger.debug("MQTT smart plug %s: energy_total=%s", plug_id, data.energy_total)
                    except (ValueError, TypeError):
                        pass  # Ignore unparseable lifetime reading from MQTT

                elif data_type == "state":
                    state_str = str(raw_value)
                    # Check against configured ON value if set
                    if config.on_value:
                        # Case-insensitive comparison
                        if state_str.lower() == config.on_value.lower():
                            data.state = "ON"
                        else:
                            data.state = "OFF"
                    else:
                        # Default behavior: normalize common values
                        upper_state = state_str.upper()
                        if upper_state in ("ON", "1", "TRUE"):
                            data.state = "ON"
                        elif upper_state in ("OFF", "0", "FALSE"):
                            data.state = "OFF"
                        else:
                            data.state = state_str
                    logger.debug("MQTT smart plug %s: state=%s", plug_id, data.state)

    def _extract_json_path(self, data: dict, path: str) -> Any:
        """Extract value using dot notation (e.g., 'power_l1' or 'data.power').

        Supports simple dot notation for nested objects.
        """
        if not path:
            return None

        parts = path.split(".")
        current = data

        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None

        return current

    def _resubscribe_all(self):
        """Resubscribe to all registered topics after reconnection."""
        if not self.client or not self.connected:
            return

        with self._lock:
            for topic in self.subscriptions:
                if self.subscriptions[topic]:  # Only if there are subscribers
                    try:
                        self.client.subscribe(topic, qos=1)
                        logger.debug("MQTT smart plug: resubscribed to %s", topic)
                    except Exception as e:
                        logger.error("MQTT smart plug: failed to resubscribe to %s: %s", topic, e)

    def subscribe(
        self,
        plug_id: int,
        # Power source
        power_topic: str | None = None,
        power_path: str | None = None,
        power_multiplier: float = 1.0,
        # Energy source
        energy_topic: str | None = None,
        energy_path: str | None = None,
        energy_multiplier: float = 1.0,
        # Lifetime energy source (separate from the above — see the model)
        energy_total_topic: str | None = None,
        energy_total_path: str | None = None,
        energy_total_multiplier: float = 1.0,
        # State source
        state_topic: str | None = None,
        state_path: str | None = None,
        state_on_value: str | None = None,
        # Legacy: single topic/path/multiplier (for backward compatibility)
        topic: str | None = None,
        multiplier: float = 1.0,
    ):
        """Subscribe to MQTT topics for a plug.

        Each data type (power, energy, state) can have its own topic.
        For backward compatibility, if power_topic is not set but topic is,
        topic will be used for all data types that have paths configured.
        """
        with self._lock:
            # Initialize config for this plug
            self.plug_configs[plug_id] = {}

            # Determine topics (new fields take priority, fall back to legacy)
            effective_power_topic = power_topic or topic
            effective_energy_topic = energy_topic or topic
            effective_state_topic = state_topic or topic

            # Use new multipliers or fall back to legacy
            effective_power_mult = power_multiplier if power_multiplier != 1.0 else multiplier
            effective_energy_mult = energy_multiplier if energy_multiplier != 1.0 else multiplier

            # Configure power subscription (path is optional - empty means use raw payload)
            if effective_power_topic:
                config = MQTTDataSourceConfig(
                    topic=effective_power_topic,
                    path=power_path or "",
                    multiplier=effective_power_mult,
                )
                self.plug_configs[plug_id]["power"] = config
                self._add_subscription(plug_id, effective_power_topic, "power")

            # Configure energy subscription (path is optional - empty means use raw payload)
            if effective_energy_topic:
                config = MQTTDataSourceConfig(
                    topic=effective_energy_topic,
                    path=energy_path or "",
                    multiplier=effective_energy_mult,
                )
                self.plug_configs[plug_id]["energy"] = config
                self._add_subscription(plug_id, effective_energy_topic, "energy")

            # Configure the LIFETIME counter. Unlike the three above this
            # REQUIRES a path: a device's daily and cumulative readings usually
            # arrive in the same payload under different keys, so a bare topic
            # cannot say which one is the lifetime figure — and guessing is the
            # bug this whole source exists to remove.
            effective_total_topic = energy_total_topic or effective_energy_topic
            if effective_total_topic and energy_total_path:
                config = MQTTDataSourceConfig(
                    topic=effective_total_topic,
                    path=energy_total_path,
                    multiplier=energy_total_multiplier,
                )
                self.plug_configs[plug_id]["energy_total"] = config
                self._add_subscription(plug_id, effective_total_topic, "energy_total")

            # Configure state subscription (path is optional - empty means use raw payload)
            if effective_state_topic:
                config = MQTTDataSourceConfig(
                    topic=effective_state_topic,
                    path=state_path or "",
                    on_value=state_on_value,
                )
                self.plug_configs[plug_id]["state"] = config
                self._add_subscription(plug_id, effective_state_topic, "state")

            # Initialize data entry
            if plug_id not in self.plug_data:
                self.plug_data[plug_id] = SmartPlugMQTTData(plug_id=plug_id)

            logger.info(
                f"MQTT smart plug {plug_id}: configured with "
                f"power={effective_power_topic if power_path else None}, "
                f"energy={effective_energy_topic if energy_path else None}, "
                f"state={effective_state_topic if state_path else None}"
            )

    def _add_subscription(self, plug_id: int, topic: str, data_type: str):
        """Add a subscription for a plug/data_type to a topic."""
        if topic not in self.subscriptions:
            self.subscriptions[topic] = []
            # Actually subscribe if connected
            if self.client and self.connected:
                try:
                    self.client.subscribe(topic, qos=1)
                    logger.info("MQTT smart plug: subscribed to %s", topic)
                except Exception as e:
                    logger.error("MQTT smart plug: failed to subscribe to %s: %s", topic, e)

        entry = (plug_id, data_type)
        if entry not in self.subscriptions[topic]:
            self.subscriptions[topic].append(entry)

    def unsubscribe(self, plug_id: int):
        """Unsubscribe when plug is deleted/updated."""
        with self._lock:
            # Get all configs for this plug
            configs = self.plug_configs.pop(plug_id, {})
            if not configs:
                # Still clean up any stray subscriptions
                pass

            # Collect all topics this plug was subscribed to
            topics_to_check = set()
            for _data_type, config in configs.items():
                topics_to_check.add(config.topic)

            # Also scan subscriptions to remove any entries for this plug
            for topic in list(self.subscriptions.keys()):
                # Remove all entries for this plug_id
                self.subscriptions[topic] = [(pid, dtype) for pid, dtype in self.subscriptions[topic] if pid != plug_id]
                topics_to_check.add(topic)

            # Unsubscribe from topics with no more subscribers
            for topic in topics_to_check:
                if topic in self.subscriptions and not self.subscriptions[topic]:
                    del self.subscriptions[topic]
                    if self.client and self.connected:
                        try:
                            self.client.unsubscribe(topic)
                            logger.info("MQTT smart plug: unsubscribed from %s", topic)
                        except Exception as e:
                            logger.error("MQTT smart plug: failed to unsubscribe from %s: %s", topic, e)

            # Remove data
            self.plug_data.pop(plug_id, None)

    def get_plug_data(self, plug_id: int) -> SmartPlugMQTTData | None:
        """Get latest data for a plug (called by status endpoint)."""
        with self._lock:
            return self.plug_data.get(plug_id)

    def is_reachable(self, plug_id: int) -> bool:
        """Check if a plug has received data recently."""
        data = self.get_plug_data(plug_id)
        if not data:
            return False

        timeout = timedelta(minutes=self.REACHABLE_TIMEOUT_MINUTES)
        return datetime.now(timezone.utc) - data.last_seen < timeout

    # -- driver interface -------------------------------------------------
    # Same four-and-a-bit methods Tasmota / REST / Home Assistant implement, so
    # smart_plug_manager can route to this service like any other.

    async def _publish(self, topic: str, payload: str) -> bool:
        """Publish one command. Returns True if the broker accepted it.

        QoS 1 because a dropped switch command is worse than a duplicate one.
        Never retained: a retained command payload would be redelivered on every
        broker reconnect and switch the plug at an arbitrary later time.
        """
        if not self.client or not self.connected:
            logger.warning("MQTT smart plug: cannot publish to %s - not connected", topic)
            return False
        try:
            info = await asyncio.to_thread(self.client.publish, topic, payload, qos=1, retain=False)
        except Exception as e:
            logger.error("MQTT smart plug: publish to %s failed: %s", topic, e)
            return False
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error("MQTT smart plug: publish to %s rejected (rc=%s)", topic, info.rc)
            return False
        return True

    async def _switch(self, plug: Any, payload: str | None, expected_state: str) -> bool:
        """Publish a command and, where possible, confirm it took effect."""
        topic = getattr(plug, "mqtt_command_topic", None)
        if not topic:
            logger.info(
                "MQTT smart plug %s: no command topic configured - monitor-only, ignoring switch request",
                plug.id,
            )
            return False

        if not await self._publish(topic, payload or ""):
            return False

        # Without a state topic there is nothing to confirm against.
        if not getattr(plug, "mqtt_state_topic", None):
            return True

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.CONTROL_CONFIRM_TIMEOUT_SECONDS
        while loop.time() < deadline:
            data = self.get_plug_data(plug.id)
            if data and data.state == expected_state:
                return True
            await asyncio.sleep(0.1)

        # The broker accepted it at QoS 1; the device just hasn't reported back
        # yet. Returning False here would mark a healthy plug as failed.
        logger.warning(
            "MQTT smart plug %s: no state confirmation within %ss (command was delivered)",
            plug.id,
            self.CONTROL_CONFIRM_TIMEOUT_SECONDS,
        )
        return True

    async def turn_on(self, plug: Any) -> bool:
        """Turn on the plug. Returns True if the command was delivered."""
        return await self._switch(plug, getattr(plug, "mqtt_command_on", None), "ON")

    async def turn_off(self, plug: Any) -> bool:
        """Turn off the plug. Returns True if the command was delivered."""
        return await self._switch(plug, getattr(plug, "mqtt_command_off", None), "OFF")

    async def toggle(self, plug: Any) -> bool:
        """Flip the plug based on its last reported state.

        Deliberately refuses when the state is unknown. The other drivers can
        toggle blind because their transport answers back; here the only source
        of truth is the state topic, and switching a printer's mains on a guess
        is worse than declining.
        """
        data = self.get_plug_data(plug.id)
        if not data or data.state not in ("ON", "OFF"):
            logger.warning(
                "MQTT smart plug %s: cannot toggle - no known state (configure a state topic)",
                plug.id,
            )
            return False
        return await (self.turn_off(plug) if data.state == "ON" else self.turn_on(plug))

    async def get_status(self, plug: Any) -> dict:
        """Current state from the subscription cache."""
        data = self.get_plug_data(plug.id)
        if not data:
            return {"state": None, "reachable": False, "device_name": None}
        return {"state": data.state, "reachable": self.is_reachable(plug.id), "device_name": None}

    async def get_energy(self, plug: Any) -> dict | None:
        """Energy data from the cache.

        ``today`` and ``total`` are deliberately distinct: only ``total`` may
        feed ``smart_plug_energy_snapshots``, so a plug with no lifetime source
        must leave it absent and keep being skipped there.
        """
        data = self.get_plug_data(plug.id)
        if not data:
            return None

        energy: dict[str, float] = {}
        if data.power is not None:
            energy["power"] = data.power
        if data.energy is not None:
            energy["today"] = data.energy
        if data.energy_total is not None:
            energy["total"] = data.energy_total
        return energy or None

    async def disconnect(self, timeout: float = 0):
        """Disconnect from MQTT broker."""
        if self.client:
            try:
                self._disconnection_event = threading.Event()
                self.client.disconnect()
                await asyncio.to_thread(self._disconnection_event.wait, timeout=timeout)
                self.client.loop_stop()
            except Exception as e:
                logger.debug("MQTT smart plug disconnect error (ignored): %s", e)
            finally:
                self.client = None
                self.connected = False


def subscribe_plug_to_mqtt(service: "MQTTSmartPlugService", plug: Any) -> list[str]:
    """Resolve per-type topic fields on a SmartPlug and register it with the service.

    The SmartPlug model carries both a legacy single ``mqtt_topic`` and newer
    per-type ``mqtt_{power,energy,state}_topic`` fields. Three code paths used
    to open-code this resolution (startup restore, create, update) and they
    drifted — the startup path skipped plugs that only had per-type topics
    set, leaving them unsubscribed after every restart (#1010). Funnelling
    all three through this helper keeps them in sync.

    Returns the list of topics subscribed (empty if nothing was configured).
    """
    power_topic = plug.mqtt_power_topic or plug.mqtt_topic
    energy_topic = plug.mqtt_energy_topic or plug.mqtt_topic
    state_topic = plug.mqtt_state_topic or plug.mqtt_topic
    energy_total_topic = plug.mqtt_energy_total_topic or energy_topic

    # Counted in the guard so a plug configured with ONLY a lifetime source is
    # still subscribed. The API validator does not currently allow that shape,
    # but the helper is also called from the startup restore path, where the
    # row comes straight from the database.
    if not (power_topic or energy_topic or state_topic or energy_total_topic):
        return []

    legacy_mult = plug.mqtt_multiplier or 1.0
    service.subscribe(
        plug_id=plug.id,
        power_topic=power_topic,
        power_path=plug.mqtt_power_path,
        power_multiplier=plug.mqtt_power_multiplier or legacy_mult,
        energy_topic=energy_topic,
        energy_path=plug.mqtt_energy_path,
        energy_multiplier=plug.mqtt_energy_multiplier or legacy_mult,
        energy_total_topic=energy_total_topic,
        energy_total_path=plug.mqtt_energy_total_path,
        energy_total_multiplier=plug.mqtt_energy_total_multiplier or 1.0,
        state_topic=state_topic,
        state_path=plug.mqtt_state_path,
        state_on_value=plug.mqtt_state_on_value,
    )
    return [t for t in {power_topic, energy_topic, state_topic, energy_total_topic} if t]


# Global instance
mqtt_smart_plug_service = MQTTSmartPlugService()
