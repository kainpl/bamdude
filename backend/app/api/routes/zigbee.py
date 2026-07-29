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
from dataclasses import asdict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.app.core.auth import RequirePermission
from backend.app.core.permissions import Permission
from backend.app.models.user import User
from backend.app.services.zigbee.coordinator import CoordinatorState, zigbee_coordinator
from backend.app.services.zigbee.devices import describe_device

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


class PermitRequest(BaseModel):
    # 1..254 on purpose. zigpy takes a byte, and 255 means "permanently open" in
    # the Zigbee spec — capping below it stops a UI that rounds up from leaving
    # the network joinable forever, which is a security property, not a detail.
    seconds: int = Field(default=60, ge=1, le=254)


def _require_up():
    """The live application, or a 409 explaining why there is not one.

    Refusing here rather than letting the call proceed matters: ``permit`` on a
    dead radio returns cleanly and then does nothing for the whole window, so
    the operator watches a countdown that was never going to work.
    """
    status = zigbee_coordinator.status
    app = zigbee_coordinator.app
    if status.state is not CoordinatorState.UP or app is None:
        raise HTTPException(
            status_code=409,
            detail=status.reason or f"The Zigbee coordinator is {status.state.value}.",
        )
    return app


@router.post("/permit")
async def permit_join(
    body: PermitRequest,
    _: User | None = RequirePermission(Permission.SMART_PLUGS_CREATE),
):
    """Open the join window. Pairing a plug is creating one, hence CREATE."""
    app = _require_up()
    await app.permit(time_s=body.seconds)
    logger.info("Zigbee join window open for %ss", body.seconds)
    return {"seconds": body.seconds}


@router.get("/devices")
async def list_devices(
    _: User | None = RequirePermission(Permission.SMART_PLUGS_READ),
):
    """Devices on the network.

    Read straight from zigpy's device table rather than a table of ours. A
    second copy would drift the moment a device leaves by any path but ours —
    and this phase deliberately creates no rows at all; phase 3 does that when a
    device is bound to a printer.

    A coordinator that is down yields an empty list, not an error: there are
    genuinely no devices to report, and the status endpoint already says why.
    """
    app = zigbee_coordinator.app
    if app is None:
        return {"devices": []}
    # The coordinator sits in zigpy's device table alongside real devices, and
    # the Dongle-M advertises an On/Off cluster — so without this filter the
    # radio shows up as a pairable plug. Filtered here rather than only marked
    # is_plug=false: an entry the operator cannot act on is noise, and phase 4
    # would have to special-case it in the UI instead.
    described = (describe_device(d) for d in app.devices.values())
    return {"devices": [asdict(info) for info in described if not info.is_coordinator]}


@router.delete("/devices/{ieee}")
async def remove_device(
    ieee: str,
    _: User | None = RequirePermission(Permission.SMART_PLUGS_DELETE),
):
    """Remove a device from the network.

    Present in this phase because pairing without unpairing means the only way
    to undo a mistake is to wipe the network and re-pair everything.
    """
    app = _require_up()
    # Case-insensitive: zigpy stringifies EUI64 lower-case, but a UI will echo
    # whatever the operator typed or pasted.
    wanted = ieee.strip().lower()
    match = next(
        (d for k, d in app.devices.items() if str(k).lower() == wanted and not describe_device(d).is_coordinator),
        None,
    )
    # The coordinator is deliberately unmatchable: remove() on our own radio
    # would take the network down with it.
    if match is None:
        raise HTTPException(status_code=404, detail=f"No Zigbee device with address {ieee}.")

    await app.remove(match.ieee)
    logger.info("Removed Zigbee device %s from the network", ieee)
    return {"removed": str(match.ieee)}
