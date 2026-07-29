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
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

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

    @property
    def status(self) -> CoordinatorStatus:
        return self._status

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
            self._status = CoordinatorStatus(CoordinatorState.ERROR, str(exc))
            logger.warning("Zigbee coordinator not started: %s", exc)
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
            self._status = CoordinatorStatus(CoordinatorState.ERROR, str(exc))
            logger.warning("Zigbee coordinator failed to start on %s: %s", device, exc)
            return

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
                logger.warning("Zigbee shutdown raised, continuing: %s", exc)
        self._lock.release()
        self._status = CoordinatorStatus(CoordinatorState.DISABLED)

    async def _open_radio(self, device: str):
        """The only place that talks to zigpy. Isolated so the lifecycle is testable.

        ``auto_form`` creates a network on the very first start; afterwards the
        database is adopted, which is what makes a restore reuse the existing
        network instead of orphaning every paired device.
        """
        from bellows.zigbee.application import ControllerApplication

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        # The plain dict is passed deliberately — do NOT pre-run it through
        # ``ControllerApplication.SCHEMA``. ``new()`` validates internally, and
        # validating twice breaks on the OTA section: the first pass turns the
        # provider entries into ``ZigpyOtaProvider`` objects, and the second
        # pass fails with "'ZigpyOtaProvider' object has no attribute 'get'"
        # because ``cv_ota_provider`` expects the dict form it already
        # converted. The error names OTA and mentions neither config nor the
        # radio, which is what made it look like a dongle fault.
        return await ControllerApplication.new(
            {
                "database_path": str(self.database_path),
                "device": {"path": device},
            },
            auto_form=True,
            start_radio=True,
        )


def _default_data_dir() -> Path:
    from backend.app.core.paths import resolve_data_dir

    return resolve_data_dir()


zigbee_coordinator = ZigbeeCoordinator(data_dir=_default_data_dir())
