"""Zigbee coordinator status and serial-port discovery.

Read-only, and small on purpose: this phase brings the radio up and says whether
it came up. Pairing is phase 2, plug control is phase 3.

Guarded by ``SMART_PLUGS_READ`` rather than a new permission. The coordinator is
smart-plug infrastructure, and every new ``Permission`` has to be mapped in
``core/auth.py`` and seeded to Administrators in a migration — this phase
deliberately ships no migration, and inventing a permission it cannot seed would
leave existing installs unable to use their own admin account for it.
"""

import asyncio
import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
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
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SMART_PLUGS_READ),
):
    """Whether the coordinator is up, and why not when it is not.

    ``reason`` is the whole explanation until the phase-4 UI exists, so it is
    returned verbatim rather than mapped to a code.
    """
    status = zigbee_coordinator.status
    identity = _radio_identity()
    return {
        "state": status.state.value,
        "reason": status.reason,
        **identity,
        "radio_changed": await _radio_changed(db, identity),
    }


async def _radio_changed(db: AsyncSession, identity: dict) -> str | None:
    """The IEEE we used to run on, when the dongle answering now is a different one.

    A dongle carries its network with it. Point the path at a second physical
    stick and everything still reports healthy — the radio comes up, the channel
    is fine, the state is ``up`` — but the paired devices are simply not there,
    because they were never on THIS one's network. Every plug reads unreachable
    at once, which looks exactly like a failure of BamDude rather than a swap the
    operator performed deliberately.

    So the previous identity is remembered and compared. Read-only when it
    matches; the first successful connection writes it, which is also how an
    install that predates this gets adopted rather than warned at.

    Returns the OLD address so the UI can name it. ``None`` means nothing to say.
    """
    coordinator = identity.get("coordinator")
    if not coordinator or not coordinator.get("ieee"):
        return None

    from backend.app.api.routes.settings import get_setting, set_setting

    current = str(coordinator["ieee"]).strip().lower()
    known = (await get_setting(db, "zigbee_radio_ieee") or "").strip().lower()

    if not known:
        await set_setting(db, "zigbee_radio_ieee", current)
        await db.commit()
        return None

    return None if known == current else known


def _radio_identity() -> dict:
    """The coordinator's own identity, and the network it runs.

    Lives on /status rather than in /devices because the radio is not something
    the operator manages — /devices answers "what can I pair and control", and
    an entry there that cannot be acted on is noise. But "which dongle am I
    actually talking to, and on which channel" is real diagnostic value, and
    without this it would be unreachable through the API entirely.

    Fields are listed explicitly, never dumped wholesale: ``network_info`` also
    carries ``network_key`` and ``tc_link_key``. Those are the secrets whose
    loss means re-pairing every device — serialising them into a status
    endpoint any reader can reach would be far worse than losing them.
    """
    app = zigbee_coordinator.app
    if app is None:
        return {"coordinator": None, "network": None}

    try:
        node = app.state.node_info
        net = app.state.network_info
        return {
            "coordinator": {
                "ieee": str(node.ieee),
                "nwk": node.nwk,
                "model": node.model,
                "manufacturer": node.manufacturer,
                "version": node.version,
            },
            "network": {"channel": net.channel, "pan_id": net.pan_id},
        }
    except Exception as exc:  # noqa: BLE001 — status must answer even when zigpy cannot
        logger.warning("Zigbee radio identity unavailable: %s", exc)
        return {"coordinator": None, "network": None}


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


async def _stop_radio() -> None:
    """Stop the coordinator and drop the listeners tied to its cluster objects.

    Shared by disconnect and forget so neither can acquire the half-stopped
    shape ``/restart`` had to be taught about: a stopped application leaves every
    cached listener pointing at orphaned clusters, and a later start would carry
    on switching plugs while reports silently never arrived.
    """
    from backend.app.services.zigbee.driver import zigbee_smart_plug_service

    await zigbee_coordinator.stop()
    await zigbee_smart_plug_service.cancel_refreshes()
    zigbee_smart_plug_service._listeners.clear()  # noqa: SLF001 — see docstring


@router.post("/disconnect")
async def disconnect_coordinator(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SMART_PLUGS_UPDATE),
):
    """Put the radio down and leave everything else alone. Fully reversible.

    The counterpart of Connect, and deliberately NOT the counterpart of pairing:
    the network database, the paired devices and every ``SmartPlug`` row survive
    untouched. Connecting again brings all of it back with nothing to
    reconfigure — which is exactly why this must be a separate action from
    forgetting the network, whose cost is a walk to every plug in the building.

    ``zigbee_enabled`` is cleared as well as the radio stopped. Stopping without
    clearing it would last until the next application start, and the operator
    who switched Zigbee off would find it back on after a restart with no
    explanation.
    """
    from backend.app.api.routes.settings import set_setting

    async with _restart_lock:
        await _stop_radio()
        await set_setting(db, "zigbee_enabled", "false")
        await db.commit()

    logger.info("Zigbee coordinator disconnected by request")
    status = zigbee_coordinator.status
    return {"state": status.state.value, "reason": status.reason, **_radio_identity()}


@router.delete("/network")
async def forget_network(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SMART_PLUGS_DELETE),
):
    """Erase the Zigbee network. **Irreversible without a backup.**

    ``zigbee.db`` holds the network key. Deleting it does not un-pair anything at
    the devices' end — it destroys our half of the relationship, so every plug
    keeps believing it belongs to a network we can no longer speak to. The only
    way back is to press the button on each one in turn, physically, wherever it
    happens to be installed.

    The one real escape hatch is a backup: ``_stage_zigbee_db`` has carried this
    file since phase 1, precisely so this mistake is survivable. Said in the
    dialog rather than only here.

    ``SmartPlug`` rows are deliberately kept. They read as unreachable, exactly
    as an unplugged dongle already makes them read, and they hold the printer
    binding, the schedules and the link to archived per-print energy. Re-pairing
    a plug reuses its IEEE, so those rows come back to life on their own with
    nothing to set up again — the same soft-retire reasoning as archived
    printers. The count is returned so the dialog can say how many are waiting.

    Sidecars go too. zigpy runs SQLite in WAL, so a freshly formed network lives
    in ``zigbee.db-wal`` while the main file is still an empty header — that is
    measured, not assumed (it is why backups snapshot rather than copy). Deleting
    only the main file would leave the network half-alive and the next start
    reading a database nobody meant to keep.
    """
    from backend.app.models.smart_plug import SmartPlug

    async with _restart_lock:
        await _stop_radio()

        db_path = zigbee_coordinator.database_path
        removed = []
        for path in (db_path, db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")):
            try:
                if path.exists():
                    path.unlink()
                    removed.append(path.name)
            except OSError as exc:
                logger.warning("Could not remove %s: %s", path.name, exc)
                raise HTTPException(500, f"Could not remove {path.name}: {exc}") from exc

        orphaned = (await db.execute(select(SmartPlug).where(SmartPlug.plug_type == "zigbee"))).scalars().all()

        # The radio identity is the network's, not the dongle's alone — keeping
        # it would make the next connection to a rebuilt network look like a
        # swapped dongle.
        from backend.app.api.routes.settings import set_setting

        await set_setting(db, "zigbee_radio_ieee", "")
        await db.commit()

    logger.warning(
        "Zigbee network erased (%s); %s plug row(s) kept and now unreachable",
        ", ".join(removed) or "nothing to remove",
        len(orphaned),
    )
    return {
        "removed": removed,
        "plugs_kept": len(orphaned),
        "state": zigbee_coordinator.status.state.value,
    }


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


# One restart at a time. The radio lock is a *file* lock and only distinguishes
# processes, so it cannot see a second call inside this one.
_restart_lock = asyncio.Lock()


@router.post("/restart")
async def restart_coordinator(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SMART_PLUGS_UPDATE),
):
    """Stop and start the coordinator with the settings as they stand now.

    Without this, enabling Zigbee in Settings does nothing until the whole
    application restarts — the coordinator is only started from the FastAPI
    lifespan. Asking an operator to restart a print farm's server to switch a
    feature on is not an acceptable first experience.

    ``zigbee_enabled`` being false is **not** an error here: ``start`` is a no-op
    and the answer is ``disabled``. Reporting a failure for a correctly-off
    feature would be the wrong signal, and this endpoint is also how the
    coordinator gets stopped after the box is unticked.
    """
    from backend.app.api.routes.settings import get_setting
    from backend.app.models.smart_plug import SmartPlug
    from backend.app.services.zigbee import reporting
    from backend.app.services.zigbee.driver import zigbee_smart_plug_service

    settings = {
        key: (await get_setting(db, key) or "") for key in ("zigbee_enabled", "zigbee_transport", "zigbee_path")
    }

    async with _restart_lock:
        await zigbee_coordinator.stop()

        # Every cached listener belongs to a cluster object that stop() just
        # orphaned. Keeping them would leave reports silently unwired while
        # commands and polling carried on working — the shape of half-broken
        # this subsystem keeps rediscovering. A read still in flight holds those
        # same orphaned clusters, so it goes with them.
        await zigbee_smart_plug_service.cancel_refreshes()
        zigbee_smart_plug_service._listeners.clear()

        await zigbee_coordinator.start(settings)

        if zigbee_coordinator.app is not None:
            rows = (await db.execute(select(SmartPlug).where(SmartPlug.plug_type == "zigbee"))).scalars().all()
            if rows:
                # Resolved through the module so a test can patch it, and so the
                # import cannot go stale against a reload.
                wired = await reporting.subscribe_all(zigbee_smart_plug_service, rows)
                logger.info("Zigbee reporting re-established for %s/%s plug(s)", wired, len(rows))

    status = zigbee_coordinator.status
    return {"state": status.state.value, "reason": status.reason, **_radio_identity()}


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


@router.get("/devices/{ieee}/attributes")
async def read_device_attributes(
    ieee: str,
    _: User | None = RequirePermission(Permission.SMART_PLUGS_READ),
):
    """Raw dump of every measurement attribute a plug exposes.

    A diagnostic, not a feature. It exists because the driver reported a
    confident wattage that turned out not to track the load at all, and no
    amount of reading our own code could say which register the device was
    actually answering with. This reads them all, unscaled and scaled, so the
    one that moves when a load is switched can be identified instead of guessed.

    Every attribute is read individually: an unsupported one makes a whole batch
    read fail, and the unsupported ones are exactly what needs to be visible.
    """
    app = _require_up()
    wanted = ieee.strip().lower()
    device = next((d for k, d in app.devices.items() if str(k).lower() == wanted), None)
    if device is None:
        raise HTTPException(status_code=404, detail=f"No Zigbee device with address {ieee}.")

    candidates = {
        0x0006: ["on_off"],
        0x0002: ["current_temperature"],
        0x0702: [
            "current_summ_delivered",
            "instantaneous_demand",
            "multiplier",
            "divisor",
            "unit_of_measure",
            "summation_formatting",
            "demand_formatting",
            "metering_device_type",
        ],
        0x0B04: [
            "active_power",
            "apparent_power",
            "rms_voltage",
            "rms_current",
            "ac_power_multiplier",
            "ac_power_divisor",
            "power_multiplier",
            "power_divisor",
            "ac_voltage_multiplier",
            "ac_voltage_divisor",
            "ac_current_multiplier",
            "ac_current_divisor",
            "measurement_type",
        ],
    }

    out: dict[str, dict] = {}
    for endpoint_id, endpoint in (getattr(device, "endpoints", None) or {}).items():
        for cluster_id, names in candidates.items():
            cluster = (getattr(endpoint, "in_clusters", None) or {}).get(cluster_id)
            if cluster is None:
                continue
            values: dict[str, object] = {"__class__": type(cluster).__name__}
            for name in names:
                try:
                    result = await cluster.read_attributes([name], allow_cache=False, only_cache=False)
                    read = result[0] if isinstance(result, (list, tuple)) else result
                    raw = repr((read or {}).get(name))
                except Exception as exc:  # noqa: BLE001 — an unsupported attribute is the answer, not an error
                    raw = f"unreadable: {exc}"
                # Raw and cached side by side. They differ exactly where a quirk
                # intervened, which is the only way to tell a quirk that is
                # working from one that silently did not match.
                try:
                    cached = repr(cluster.get(name))
                except Exception as exc:  # noqa: BLE001
                    cached = f"uncached: {exc}"
                values[name] = {"raw": raw, "cached": cached}
            out[f"ep{endpoint_id}/0x{cluster_id:04X}"] = values

    # Which quirk (if any) zigpy resolved this device to, and the firmware
    # version the quirk registry filtered on. Both are here because a quirk that
    # silently does not match looks exactly like a quirk that does not exist, and
    # the difference decides whether a wrong reading is the device's fault or
    # ours.
    quirk = getattr(device, "quirk_registry_entry", None) or getattr(device, "_quirk_registry_entry", None)
    firmware = getattr(device, "firmware_version", None)
    return {
        "ieee": str(device.ieee),
        "manufacturer": getattr(device, "manufacturer", None),
        "model": getattr(device, "model", None),
        "device_class": type(device).__name__,
        "firmware_version": f"0x{firmware:08X}" if isinstance(firmware, int) else repr(firmware),
        "quirk": repr(quirk) if quirk is not None else None,
        "attributes": out,
    }
