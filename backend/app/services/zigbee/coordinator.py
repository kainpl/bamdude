"""Owns the Zigbee radio: lifecycle and status, nothing device-shaped.

Pairing (phase 2) and control (phase 3) live elsewhere on purpose. The moment
this class grows a device API, the scope stops being "plugs" and starts being a
Zigbee2MQTT rewrite.

**Nothing escapes** :meth:`ZigbeeCoordinator.start`. A dongle that is unplugged,
held by another process, or left in Router mode must leave BamDude fully usable
with an explanation in the Zigbee status — the same posture the app already
takes for a printer that will not connect. An exception here would let a $20 USB
stick stop the whole farm from loading, which is a far worse outcome than not
having Zigbee.

Everything that talks to zigpy is confined to :meth:`_open_radio`. That one seam
is why the lifecycle is testable without hardware.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from backend.app.core.tasks import spawn_background_task
from backend.app.core.websocket import ws_manager
from backend.app.services.zigbee.devices import DeviceKind, describe_device, describe_for_ui
from backend.app.services.zigbee.errors import describe_exception
from backend.app.services.zigbee.radio_lock import RadioLock
from backend.app.services.zigbee.transport import TransportConfigError, resolve_transport

logger = logging.getLogger(__name__)

_BUSY_RADIO_REASON = (
    "The Zigbee radio is already in use. Another BamDude instance, or Zigbee2MQTT / Home Assistant, may be holding it."
)


class CoordinatorState(str, Enum):
    DISABLED = "disabled"
    STARTING = "starting"
    UP = "up"
    ERROR = "error"


@dataclass(frozen=True)
class CoordinatorStatus:
    state: CoordinatorState
    reason: str = ""


class ZigbeeCoordinator:
    def __init__(self, data_dir: Path | str):
        self._data_dir = Path(data_dir)
        self._lock = RadioLock(self._data_dir / "zigbee" / "radio.lock")
        self._app = None
        self._status = CoordinatorStatus(CoordinatorState.DISABLED)
        # What each sensor was last configured with, and what it accepted. Held
        # in memory: after a restart the applied state is unknown, which the
        # next contact repairs — configure_reporting is idempotent, so
        # re-issuing when in doubt is cheap and correct.
        self._desired_reporting: dict[str, dict[str, dict]] = {}
        self._applied_reporting: dict[str, dict[str, str]] = {}

    @property
    def status(self) -> CoordinatorStatus:
        return self._status

    @property
    def app(self):
        """The live ``ControllerApplication``, or None when the radio is not up.

        A narrow accessor rather than callers reaching into ``_app``: phase 1
        deliberately exposed no device surface at all, and keeping the widening
        to one reviewed place is what stops every new route taking a private
        attribute and quietly depending on the internals.
        """
        return self._app

    @property
    def database_path(self) -> Path:
        """zigpy's own SQLite device database.

        Its own subdirectory, never beside ``bamdude.db``: zigpy uses SQLite even
        when BamDude is on PostgreSQL, and the separation is what keeps anyone
        from mistaking it for our application database or feeding it to the
        migration chain. It also holds the network key, which is why the backup
        carries this file (see ``create_backup_zip``).
        """
        return self._data_dir / "zigbee" / "zigbee.db"

    async def start(self, settings: dict[str, str]) -> None:
        """Bring the radio up. Never raises — see the module docstring."""
        if self._app is not None:
            return  # idempotent: --reload and double-start are both no-ops

        if (settings.get("zigbee_enabled") or "").strip().lower() != "true":
            self._status = CoordinatorStatus(CoordinatorState.DISABLED)
            return

        try:
            device = resolve_transport(
                settings.get("zigbee_transport") or "ethernet",
                settings.get("zigbee_path") or "",
            )
        except TransportConfigError as exc:
            # Before the lock on purpose: a misconfigured install must not leave
            # a lock file implying it ever owned the radio.
            self._status = CoordinatorStatus(CoordinatorState.ERROR, describe_exception(exc))
            logger.warning("Zigbee coordinator not started: %s", describe_exception(exc))
            return

        if not self._lock.acquire():
            self._status = CoordinatorStatus(CoordinatorState.ERROR, _BUSY_RADIO_REASON)
            return

        self._status = CoordinatorStatus(CoordinatorState.STARTING)
        try:
            self._app = await self._open_radio(device)
        except Exception as exc:  # noqa: BLE001 — the contract is that nothing escapes
            self._lock.release()
            self._app = None
            self._status = CoordinatorStatus(CoordinatorState.ERROR, describe_exception(exc))
            logger.warning("Zigbee coordinator failed to start on %s: %s", device, describe_exception(exc))
            return

        # Registered here rather than inside _open_radio so that seam stays
        # purely "talk to zigpy" and the listener wiring is visible in the
        # lifecycle, where anyone reading start() will see it.
        self._app.add_listener(self)

        self._status = CoordinatorStatus(CoordinatorState.UP)
        logger.info("Zigbee coordinator up on %s", device)

    async def stop(self) -> None:
        """Release everything. Safe without a start, and safe twice.

        Runs during application teardown, so a radio that has already gone away
        must not take the teardown down with it.
        """
        app, self._app = self._app, None
        if app is not None:
            try:
                await app.shutdown()
            except Exception as exc:  # noqa: BLE001 — teardown must complete
                logger.warning("Zigbee shutdown raised, continuing: %s", describe_exception(exc))
        self._lock.release()
        self._status = CoordinatorStatus(CoordinatorState.DISABLED)

    # ---- zigpy listener callbacks -------------------------------------------
    #
    # Two properties of zigpy's dispatch shape everything below, and both make
    # mistakes here SILENT rather than loud:
    #
    # 1. ``listener_event`` invokes callbacks with ``method(*args)`` and never
    #    awaits. An ``async def`` callback would return a coroutine nobody runs,
    #    so every broadcast would simply never happen — no error, no log. Hence
    #    plain ``def`` throughout, with work handed to ``spawn_background_task``.
    # 2. zigpy already wraps callbacks in ``except Exception`` and logs at DEBUG.
    #    A raising callback therefore does not destabilise the stack; it
    #    disappears. The guards below exist to make failures visible at WARNING,
    #    not to protect zigpy.

    def _emit(self, message: dict) -> None:
        """Schedule a WebSocket broadcast from a synchronous zigpy callback."""
        try:
            spawn_background_task(ws_manager.broadcast(message), name=f"zigbee-{message['type']}")
        except Exception as exc:  # noqa: BLE001 — see the block comment above
            logger.warning("Zigbee event %s not broadcast: %s", message.get("type"), describe_exception(exc))

    async def _bind_sensor(self, device, ieee: str) -> None:
        """Configure a freshly paired sensor's reporting, with the operator's
        parameters if any are set.

        Never raises into pairing: a device that joined but could not be
        configured is still paired, and the API says its reporting is refused
        rather than pretending it works.
        """
        from backend.app.core.database import async_session
        from backend.app.services.zigbee.reporting import bind_sensor
        from backend.app.services.zigbee.sensor_settings import load_reporting_parameters

        try:
            async with async_session() as db:
                parameters = await load_reporting_parameters(db)
            applied = await bind_sensor(device, ieee, parameters)
            info = describe_device(device)
            self.record_reporting(ieee, {key: parameters.get(key, {}) for key in info.measurements}, applied)
            logger.info("Zigbee sensor %s reporting: %s", ieee, applied)
        except Exception as exc:  # noqa: BLE001 — see the docstring
            logger.warning("Zigbee sensor %s: binding failed: %s", ieee, describe_exception(exc))

    def applied_reporting(self, ieee: str) -> dict[str, str]:
        """What a sensor actually accepted, per measurement.

        Empty after a restart, which readers render as "pending" rather than as
        failure — we genuinely do not know until the device is next contacted.
        """
        return self._applied_reporting.get(str(ieee).strip().lower(), {})

    def desired_reporting(self, ieee: str) -> dict[str, dict]:
        """The parameters the last successful configure was made with — what a
        settings change is compared against to decide a re-apply is owed."""
        return self._desired_reporting.get(str(ieee).strip().lower(), {})

    def record_reporting(self, ieee: str, desired: dict[str, dict], applied: dict[str, str]) -> None:
        key = str(ieee).strip().lower()
        self._desired_reporting[key] = desired
        self._applied_reporting[key] = applied

    def forget_reporting(self, ieee: str) -> None:
        """Drop what we knew about an unpaired sensor's reporting."""
        key = str(ieee).strip().lower()
        self._desired_reporting.pop(key, None)
        self._applied_reporting.pop(key, None)

    def device_joined(self, device) -> None:
        """A device announced itself; the interview has not run yet.

        Announced separately from :meth:`device_initialized` because
        interviewing can take tens of seconds, and a UI that shows nothing for
        that long looks broken rather than busy.
        """
        try:
            self._emit({"type": "zigbee_device_joining", "ieee": str(device.ieee)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Zigbee device_joined handler failed: %s", describe_exception(exc))

    def device_initialized(self, device) -> None:
        """Interview complete — accept it, or remove it and say why."""
        try:
            info = describe_device(device)
            if info.kind in (DeviceKind.PLUG, DeviceKind.SENSOR):
                logger.info("Paired Zigbee %s %s (%s)", info.kind.value, info.ieee, info.model)
                self._emit({"type": "zigbee_device_paired", "device": describe_for_ui(info)})
                if info.kind is DeviceKind.SENSOR:
                    # Bound here and nowhere else: this is the one moment a
                    # battery sensor is provably awake. Binding it blindly at
                    # every startup instead would be spent radio and an empty
                    # log, because by then it is asleep again.
                    spawn_background_task(self._bind_sensor(device, info.ieee), name=f"zigbee-bind-{info.ieee}")
                return

            # Removed, not merely ignored: a device left joined but unusable
            # occupies a network address and reappears in every device list,
            # indistinguishable from a plug that failed for another reason.
            logger.info("Rejecting unsupported Zigbee device %s (%s)", info.ieee, info.model)
            self._emit({"type": "zigbee_device_rejected", "device": describe_for_ui(info)})
            if self._app is not None:
                spawn_background_task(self._app.remove(device.ieee), name="zigbee-remove-non-plug")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Zigbee device_initialized handler failed: %s", describe_exception(exc))

    def device_left(self, device) -> None:
        try:
            self._emit({"type": "zigbee_device_left", "ieee": str(device.ieee)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Zigbee device_left handler failed: %s", describe_exception(exc))

    def connection_lost(self, exc: Exception) -> None:
        """The radio is gone.

        Phase 1 left ``status`` as a startup snapshot and deferred liveness to
        phase 3. This costs one line here because the listener exists anyway,
        and without it pairing would run against a dead radio while the status
        endpoint still reported ``up``.
        """
        try:
            self._status = CoordinatorStatus(
                CoordinatorState.ERROR,
                f"Connection to the Zigbee radio was lost: {describe_exception(exc)}",
            )
            logger.warning("Zigbee radio connection lost: %s", describe_exception(exc))
            self._emit(
                {
                    "type": "zigbee_status_changed",
                    "state": self._status.state.value,
                    "reason": self._status.reason,
                }
            )
        except Exception as inner:  # noqa: BLE001
            logger.warning("Zigbee connection_lost handler failed: %s", inner)

    async def _open_radio(self, device: str):
        """The only place that talks to zigpy. Isolated so the lifecycle is testable.

        ``auto_form`` creates a network on the very first start; afterwards the
        database is adopted, which is what makes a restore reuse the existing
        network instead of orphaning every paired device.

        ``device_resolver`` is what applies per-model quirks, and without it
        zigpy applies **none** — ``_resolve_device`` returns the bare device when
        no resolver is registered. That is not a detail. The plug this was
        developed against runs a firmware that keeps reporting the last measured
        power and current after its socket is switched off, so BamDude read 33 W
        from a socket with nothing plugged in and switched off; the quirk for it
        zeroes those readings on switch-off and blocks further updates until the
        socket is on again. Home Assistant's ZHA passes the same resolver
        (``zha/application/gateway.py``: ``device_resolver=DEVICE_REGISTRY.resolve``).

        Built step by step instead of via ``ControllerApplication.new()``, and the
        reason is teardown. ``new()`` holds the application in a local: it calls
        ``_load_db()`` (which starts ``PersistingListener._worker``) and then
        ``startup()``, and if ``startup()`` raises, the object is dropped on the
        floor. Nobody can shut down what was never returned, so bellows' reader
        thread and zigpy's DB worker outlive the failure — the process stops
        exiting, and every retry leaks another set. Observed as "Task was
        destroyed but it is pending" and, in a repro here, an interpreter that
        printed its last line and then hung.

        zigpy's own ``startup()`` does call ``shutdown(db=False)`` on failure, but
        ``db=False`` is exactly the half that leaves the database worker running.

        Keeping the reference lets the ``except`` below run a real ``shutdown()``.
        The sequence mirrors ``new()`` one for one; ``test_open_radio_mirrors_zigpy_new``
        fails if zigpy changes it.
        """
        from bellows.zigbee.application import ControllerApplication

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        resolver = _quirk_resolver()
        # The plain dict is passed deliberately — do NOT pre-run it through
        # ``ControllerApplication.SCHEMA``. ``new()`` validates internally, and
        # validating twice breaks on the OTA section: the first pass turns the
        # provider entries into ``ZigpyOtaProvider`` objects, and the second
        # pass fails with "'ZigpyOtaProvider' object has no attribute 'get'"
        # because ``cv_ota_provider`` expects the dict form it already
        # converted. The error names OTA and mentions neither config nor the
        # radio, which is what made it look like a dongle fault.
        app = ControllerApplication(
            {
                "database_path": str(self.database_path),
                "device": {"path": device},
            }
        )
        if resolver is not None:
            app.register_device_resolver(resolver)

        try:
            await app._load_db()  # noqa: SLF001 — mirrors ControllerApplication.new()
            await app.startup(auto_form=True)
        except BaseException:
            # Best-effort and total: db=True here on purpose, unlike zigpy's own
            # failure path. A teardown that raises must not replace the reason we
            # are here — that masking is the very bug above.
            with suppress(Exception):
                await app.shutdown()
            raise

        return app


def _quirk_resolver():
    """Register every quirk and return zigpy's device resolver, or None.

    Called once per radio start. ``zhaquirks.setup()`` imports the whole quirk
    library for its registration side effects — v1 quirks land in zigpy's legacy
    registry, v2 quirks in the one this returns — so it is not cheap, but it
    happens once and only when Zigbee is enabled.

    Returning None on failure is deliberate: quirks make readings correct, and a
    radio that comes up with uncorrected readings is still a radio the operator
    can switch printers with. Refusing to start would trade a partial feature
    for none at all.
    """
    try:
        import zhaquirks

        zhaquirks.setup()
        return zhaquirks.ZHA_DEVICE_REGISTRY.resolve
    except Exception as exc:  # noqa: BLE001 — see the docstring
        logger.warning("Zigbee device quirks unavailable, readings may be uncorrected: %s", describe_exception(exc))
        return None


def _default_data_dir() -> Path:
    from backend.app.core.paths import resolve_data_dir

    return resolve_data_dir()


zigbee_coordinator = ZigbeeCoordinator(data_dir=_default_data_dir())
