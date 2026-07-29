"""Zigbee coordinator status and serial-port discovery.

Read-only, and small on purpose: this phase brings the radio up and says whether
it came up. Pairing is phase 2, plug control is phase 3.

Guarded by ``SMART_PLUGS_READ`` rather than a new permission. The coordinator is
smart-plug infrastructure, and every new ``Permission`` has to be mapped in
``core/auth.py`` and seeded to Administrators in a migration — this phase
deliberately ships no migration, and inventing a permission it cannot seed would
leave existing installs unable to use their own admin account for it.
"""

import logging

from fastapi import APIRouter

from backend.app.core.auth import RequirePermission
from backend.app.core.permissions import Permission
from backend.app.models.user import User
from backend.app.services.zigbee.coordinator import zigbee_coordinator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/zigbee", tags=["zigbee"])


def _comports() -> list:
    """Enumerate serial ports through zigpy's own serial layer.

    ``serialx``, not ``pyserial``: zigpy opens the port with serialx, so
    enumerating with the same library guarantees the names offered to the user
    are the names zigpy will accept. A second serial library could format device
    names differently and hand the user a string that then fails to open.

    Imported here rather than at module scope on purpose. This module is
    imported by ``main.py``, so an ImportError at module level would take the
    whole application down — the exact failure the coordinator is written to
    avoid. Losing port enumeration must cost the port list, nothing more.
    """
    try:
        from serialx.tools.list_ports import comports
    except ImportError as exc:
        logger.warning("Serial port enumeration unavailable: %s", exc)
        return []
    return list(comports())


@router.get("/status")
async def get_zigbee_status(
    _: User | None = RequirePermission(Permission.SMART_PLUGS_READ),
):
    """Whether the coordinator is up, and why not when it is not.

    ``reason`` is the whole explanation until the phase-4 UI exists, so it is
    returned verbatim rather than mapped to a code.
    """
    status = zigbee_coordinator.status
    return {"state": status.state.value, "reason": status.reason}


@router.get("/ports")
async def list_serial_ports(
    _: User | None = RequirePermission(Permission.SMART_PLUGS_READ),
):
    """Candidate serial ports for USB mode.

    Exists so the UI can offer a list instead of a text field. A field where a
    Windows user is expected to type ``/dev/ttyUSB0`` is a support ticket
    waiting to happen, and the device name differs on every platform.

    A machine with no serial ports is normal, not a failure — it returns an
    empty list. Enumeration itself is best-effort: a driver that makes
    ``comports()`` raise must not turn the Zigbee settings page into a 500.
    """
    try:
        ports = [{"device": p.device, "description": p.description or "", "hwid": p.hwid or ""} for p in _comports()]
    except Exception as exc:  # noqa: BLE001 — enumeration must never 500 the page
        logger.warning("Serial port enumeration failed: %s", exc)
        ports = []
    return {"ports": ports}
