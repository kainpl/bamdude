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
import contextlib
import logging
import math

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.printer_location import PrinterLocation
from backend.app.models.smart_plug import SmartPlug
from backend.app.models.smart_sensor import SmartSensor
from backend.app.models.user import User
from backend.app.schemas.printer_location import PrinterLocationOut
from backend.app.schemas.smart_sensor import SmartSensorCreate, SmartSensorOut, SmartSensorUpdate
from backend.app.schemas.zigbee_settings import DeviceSettingsUpdate

# Imported as a module so the lock and the restart sequence are visibly the same
# objects the supervisor uses, not copies that could drift apart.
from backend.app.services.zigbee import supervisor as _supervisor
from backend.app.services.zigbee.coordinator import CoordinatorState, zigbee_coordinator
from backend.app.services.zigbee.device_settings import resolve_reporting
from backend.app.services.zigbee.devices import DeviceKind, describe_device, describe_for_ui

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
#
# ⚠️ Shared with the supervisor rather than owned here. It performs the same
# restart on its own timer, so a lock private to this module would let a retry
# run straight through an operator disconnecting the radio or resetting the
# network — and the file lock, being per-process, would not notice either.
_restart_lock = _supervisor.restart_lock


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
    async with _restart_lock:
        await _supervisor.restart_radio(db)

    status = zigbee_coordinator.status
    return {"state": status.state.value, "reason": status.reason, **_radio_identity()}


@router.get("/devices")
async def list_devices(
    db: AsyncSession = Depends(get_db),
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
    from backend.app.models.smart_sensor import SmartSensor
    from backend.app.models.zigbee_device import ZigbeeDevice

    # Adopted is computed from the entity tables, never stored beside them —
    # one question, one answer, nothing that can disagree with the rows.
    adopted = {
        str(ieee).lower()
        for (ieee,) in (
            (await db.execute(select(SmartPlug.zigbee_ieee).where(SmartPlug.zigbee_ieee.isnot(None)))).all()
            + (await db.execute(select(SmartSensor.zigbee_ieee))).all()
        )
        if ieee
    }
    # The hardware name as it was recorded when the device paired. Read from the
    # row rather than from the live device so a radio that has just come up, and
    # has not finished interviewing, still names what it knows.
    names = {
        str(ieee).lower(): name for ieee, name in (await db.execute(select(ZigbeeDevice.ieee, ZigbeeDevice.name))).all()
    }

    described = (describe_device(d) for d in app.devices.values())
    return {
        "devices": [
            {
                **describe_for_ui(info),
                "name": names.get(info.ieee.lower()),
                "adopted": info.ieee.lower() in adopted,
            }
            for info in described
            if not info.is_coordinator
        ]
    }


@router.get("/sensors")
async def list_sensors(
    db: AsyncSession = Depends(get_db),
    # Sensors, not plugs. The two permissions travel together in all three
    # default groups, so the mismatch was invisible until this list started
    # feeding the group headers on three main pages -- where a group granted
    # only smart_sensors:read would have got a 403 on every one of them.
    _: User | None = RequirePermission(Permission.SMART_SENSORS_READ),
):
    """Every adopted sensor with what it last told us.

    Built from the ``smart_sensors`` rows, with the live device enriching them.
    The other way round -- walking zigpy's table and keeping what is adopted --
    made a downed radio and a sensor that left the mesh both answer "nothing
    configured", which reads as BamDude having forgotten the device rather than
    being unable to see it.

    ``value`` is null when nothing is known -- never 0. A fabricated reading is
    worse than a missing one, which is the rule plug power already follows.
    ``stale`` says the value is older than its window, and ``reporting`` is what
    the device actually accepted, so "reporting is configured" never has to be
    inferred from silence.
    """
    from backend.app.models.zigbee_device import ZigbeeDevice
    from backend.app.services.zigbee.device_settings import resolve_reporting, resolve_stale_after_seconds
    from backend.app.services.zigbee.measurements import BY_KEY
    from backend.app.services.zigbee.reporting_targets import targets_for
    from backend.app.services.zigbee.sensors import PowerClass, power_class, sensor_store

    rows = (await db.execute(select(SmartSensor))).scalars().all()
    if not rows:
        return {"sensors": []}

    # What the radio recorded when the device paired. It is the only thing we
    # still know about a device that is no longer on the mesh.
    hardware_names = {
        str(ieee).lower(): name for ieee, name in (await db.execute(select(ZigbeeDevice.ieee, ZigbeeDevice.name))).all()
    }

    app = zigbee_coordinator.app
    sensors = []
    for row in rows:
        ieee = str(row.zigbee_ieee).lower()
        device = _find_device(app, ieee) if app is not None else None
        entry = {
            "id": row.id,
            # What the operator calls it. The hardware's own name is a
            # different question, answered by the settings endpoint.
            "name": row.name,
            # The place, resolved -- one shape for a location everywhere.
            "location": PrinterLocationOut.from_location(row.location),
            # ⚠️ Or the printer it belongs to, exclusive with the place above.
            # Both are sent so the settings list can show which binding was
            # chosen without asking a second time.
            "printer_id": row.printer_id,
            "printer_name": row.printer.name if row.printer else None,
            "ieee": ieee,
            "present": device is not None,
        }

        if device is None:
            # Not on the mesh: a downed radio, a flat cell, a device carried out
            # of range. Which quantities it would report is derived from the
            # clusters a live device carries, so there is nothing honest to put
            # in `measurements` -- inventing them would be worse than omitting.
            sensors.append(
                {
                    **entry,
                    "nwk": None,
                    "manufacturer": None,
                    "model": hardware_names.get(ieee),
                    "power": None,
                    "quirk_applied": None,
                    "unreachable": True,
                    "measurements": {},
                }
            )
            continue

        info = describe_device(device)
        parameters = await resolve_reporting(db, info)
        polled = power_class(device) is PowerClass.MAINS
        applied = zigbee_coordinator.applied_reporting(ieee)

        measurements = {}
        # From the targets, which are derived from the clusters the device
        # actually carries. Battery is in that list and NOT in
        # ``info.measurements`` -- a battery cluster alone does not make a
        # sensor -- and listing it by hand here was the second place that
        # knowledge lived.
        for target in targets_for(info):
            measurement = BY_KEY.get(target.key)
            if measurement is None:
                continue
            reading = sensor_store.reading(ieee, target.key)
            max_interval = parameters.get(target.key, {}).get("max_interval", measurement.default_max_interval)
            window = await resolve_stale_after_seconds(db, ieee, polled=polled, max_interval=max_interval)
            state = applied.get(target.key) or {}
            measurements[target.key] = {
                "value": reading.value if reading else None,
                "unit": measurement.unit,
                "last_report_at": reading.at.isoformat() if reading else None,
                "stale": sensor_store.is_stale(ieee, target.key, window, 1),
                # Two facts, not one word: what the device answered, and what
                # reading the configuration back said. Both are unknown after a
                # restart and are re-established at the next contact.
                "reporting": state.get("state", "unknown"),
                "verification": state.get("verification", "not-checked"),
            }

        sensors.append(
            {
                **entry,
                "nwk": info.nwk,
                "manufacturer": info.manufacturer,
                "model": info.model,
                "power": power_class(device).value,
                # A quirk that did not apply is invisible in the values; the
                # class name is the one place it shows.
                "quirk_applied": type(device).__name__ != "Device",
                "unreachable": sensor_store.is_unreachable(ieee),
                "measurements": measurements,
            }
        )
    return {"sensors": sensors}


class SensorThresholdIn(BaseModel):
    kind: str
    min_value: float | None = None
    max_value: float | None = None
    deadband: float = Field(default=0.0, ge=0)
    enabled: bool = True

    @field_validator("kind")
    @classmethod
    def _known_quantity(cls, value: str) -> str:
        from backend.app.services.zigbee.measurements import BY_KEY

        if value not in BY_KEY:
            raise ValueError(f"Unknown quantity: {value}.")
        return value

    @model_validator(mode="after")
    def _at_least_one_limit(self):
        # An empty demand: it could never fire, and nothing on screen could
        # explain why.
        if self.min_value is None and self.max_value is None:
            raise ValueError("A threshold needs a minimum, a maximum, or both.")
        return self


class SensorThresholdsIn(BaseModel):
    thresholds: list[SensorThresholdIn]


def _threshold_out(row) -> dict:
    from backend.app.services.zigbee.measurements import BY_KEY

    measurement = BY_KEY.get(row.kind)
    return {
        "kind": row.kind,
        "min_value": row.min_value,
        "max_value": row.max_value,
        "deadband": row.deadband,
        "enabled": row.enabled,
        # Read-only: what the last evaluation decided.
        "state": row.state,
        # So the dialog can label each field without carrying its own copy of
        # the measurement registry.
        "unit": measurement.unit if measurement else "",
    }


@router.get("/sensors/{sensor_id}/thresholds")
async def get_sensor_thresholds(
    sensor_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SMART_SENSORS_READ),
):
    """What counts as wrong for this sensor, and whether it currently is."""
    from backend.app.models.smart_sensor_threshold import SmartSensorThreshold

    if await db.get(SmartSensor, sensor_id) is None:
        raise HTTPException(status_code=404, detail="No such sensor.")

    rows = (
        (await db.execute(select(SmartSensorThreshold).where(SmartSensorThreshold.sensor_id == sensor_id)))
        .scalars()
        .all()
    )
    return {"thresholds": [_threshold_out(row) for row in rows]}


@router.put("/sensors/{sensor_id}/thresholds")
async def put_sensor_thresholds(
    sensor_id: int,
    payload: SensorThresholdsIn,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SMART_SENSORS_UPDATE),
):
    """The whole set at once, like the reporting dialog.

    A quantity absent from the body ends up with no threshold. Rows that stay
    are updated in place and **keep their alarm state**: rewriting a limit is
    not an acknowledgement, and the next evaluation decides honestly — a limit
    raised above the current reading produces a real all-clear.
    """
    from backend.app.models.smart_sensor_threshold import SmartSensorThreshold

    if await db.get(SmartSensor, sensor_id) is None:
        raise HTTPException(status_code=404, detail="No such sensor.")

    existing = {
        row.kind: row
        for row in (await db.execute(select(SmartSensorThreshold).where(SmartSensorThreshold.sensor_id == sensor_id)))
        .scalars()
        .all()
    }
    wanted = {item.kind: item for item in payload.thresholds}

    for kind, row in existing.items():
        if kind not in wanted:
            await db.delete(row)

    for kind, item in wanted.items():
        row = existing.get(kind)
        if row is None:
            row = SmartSensorThreshold(sensor_id=sensor_id, kind=kind)
            db.add(row)
        row.min_value = item.min_value
        row.max_value = item.max_value
        row.deadband = item.deadband
        row.enabled = item.enabled

    await db.commit()

    rows = (
        (await db.execute(select(SmartSensorThreshold).where(SmartSensorThreshold.sensor_id == sensor_id)))
        .scalars()
        .all()
    )
    return {"thresholds": [_threshold_out(row) for row in rows]}


@router.post("/sensors", status_code=201, response_model=SmartSensorOut)
async def adopt_sensor(
    payload: SmartSensorCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SMART_SENSORS_CREATE),
):
    """Start tracking a paired sensor, under a name the operator chooses.

    The same two steps a plug already takes: pair it, then add it. Pairing on
    its own puts a device on the network and gives its settings somewhere to
    live; it does not decide that the farm cares about it.
    """
    from backend.app.services.zigbee.device_settings import load_device_row

    ieee = payload.zigbee_ieee.strip().lower()
    row = await load_device_row(db, ieee)
    if row is None:
        raise HTTPException(status_code=404, detail="This device has never paired with BamDude.")
    if row.kind != DeviceKind.SENSOR.value:
        # The device classes are closed and separate. A plug adopted here would
        # appear in two lists with two names and no on/off anywhere.
        raise HTTPException(status_code=422, detail="This device is a plug. Add it under Smart plugs instead.")

    existing = await db.execute(select(SmartSensor.id).where(func.lower(SmartSensor.zigbee_ieee) == ieee))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="This sensor has already been added.")

    sensor = SmartSensor(name=payload.name.strip(), zigbee_ieee=ieee)
    await _bind_sensor(
        db,
        sensor,
        location_id=payload.location_id,
        printer_id=payload.printer_id,
        set_location=True,
        set_printer=True,
    )
    db.add(sensor)
    await db.commit()
    await db.refresh(sensor)
    return _sensor_out(sensor)


async def _bind_sensor(db, sensor, *, location_id, printer_id, set_location: bool, set_printer: bool) -> None:
    """Point a sensor at a place or at a printer, never at both.

    ⚠️ Exclusive by construction rather than by a check that could be forgotten:
    setting either side clears the other. The two answer the same question —
    where this reading belongs — and a printer already has a location, so a
    sensor holding both could claim a place its printer is not in and appear in
    two lists at once.

    ``set_location`` / ``set_printer`` say whether the caller mentioned the
    field at all, so an update that touches neither leaves the binding alone,
    and one that sends an explicit null unbinds.
    """
    from backend.app.models.printer import Printer

    if set_printer and printer_id is not None:
        if await db.get(Printer, printer_id) is None:
            raise HTTPException(status_code=422, detail="No such printer.")
        sensor.printer_id = printer_id
        sensor.location_id = None
        return
    if set_location and location_id is not None:
        if await db.get(PrinterLocation, location_id) is None:
            raise HTTPException(status_code=422, detail="No such location.")
        sensor.location_id = location_id
        sensor.printer_id = None
        return
    # Explicit nulls: unbind whichever side was named.
    if set_printer:
        sensor.printer_id = None
    if set_location:
        sensor.location_id = None


def _sensor_out(sensor) -> SmartSensorOut:
    """Serialise a sensor, naming the printer it is bound to.

    Read off the eager-loaded relationship, so a sensor list costs no extra
    query per row.
    """
    payload = SmartSensorOut.model_validate(sensor, from_attributes=True)
    payload.printer_name = sensor.printer.name if sensor.printer else None
    return payload


@router.patch("/sensors/{sensor_id}", response_model=SmartSensorOut)
async def rename_sensor(
    sensor_id: int,
    payload: SmartSensorUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SMART_SENSORS_UPDATE),
):
    """Change what the operator calls it, and where it stands.

    Not what the hardware calls itself: that is the radio's answer, kept in
    ``zigbee_devices`` and never edited, so the two never have to be reconciled.
    """
    sensor = await db.get(SmartSensor, sensor_id)
    if sensor is None:
        raise HTTPException(status_code=404, detail="No such sensor.")
    # `model_fields_set` distinguishes "the key was not sent" from "the key was
    # sent as null". Without it, null means "leave alone" and a sensor that was
    # once given a place can never become placeless.
    if "name" in payload.model_fields_set and payload.name is not None:
        sensor.name = payload.name.strip()
    await _bind_sensor(
        db,
        sensor,
        location_id=payload.location_id,
        printer_id=payload.printer_id,
        set_location="location_id" in payload.model_fields_set,
        set_printer="printer_id" in payload.model_fields_set,
    )
    await db.commit()
    await db.refresh(sensor)
    return _sensor_out(sensor)


@router.delete("/sensors/{sensor_id}")
async def drop_sensor(
    sensor_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SMART_SENSORS_DELETE),
):
    """Stop tracking a sensor. It stays on the network.

    Only the farm-level row goes. The device remains paired, keeps its
    reporting settings and goes on being configured, so adopting it again
    restores exactly what it had. Taking it off the network is
    ``DELETE /zigbee/devices/{ieee}`` — a different, more expensive action,
    since a sensor removed from the mesh has to be paired again in person.
    """
    sensor = await db.get(SmartSensor, sensor_id)
    if sensor is None:
        raise HTTPException(status_code=404, detail="No such sensor.")
    await db.delete(sensor)
    await db.commit()
    return {"deleted": sensor_id}


def _find_device(app, ieee: str):
    """The paired device with this address, or None.

    Case-insensitive: zigpy stringifies EUI64 lower-case, but a UI echoes
    whatever the operator typed or pasted.
    """
    wanted = str(ieee).strip().lower()
    return next((d for k, d in (getattr(app, "devices", None) or {}).items() if str(k).lower() == wanted), None)


async def _is_adopted(db, info) -> bool:
    """Whether an entity row references this device.

    Computed rather than stored: a plug's adoption has always meant "a
    smart_plugs row carries this IEEE", and a flag beside that would be a
    second source of truth for one question.
    """
    from backend.app.models.smart_sensor import SmartSensor

    model = SmartSensor if info.kind is DeviceKind.SENSOR else SmartPlug
    found = await db.execute(select(model.id).where(func.lower(model.zigbee_ieee) == info.ieee.lower()))
    return found.scalar_one_or_none() is not None


def _applied_entry(recorded: dict | None, wanted: dict | None) -> dict:
    """One measurement's outcome, plus whether it is about the CURRENT settings.

    ``describes_desired`` is the difference between "verified" and "verified,
    for what you are looking at". A sleeping sensor keeps the previous outcome
    until it next wakes, so without this the answer showed a confirmation of the
    old configuration as if it confirmed the settings just saved — the one
    failure the state/verification split exists to prevent, arriving through a
    third door.
    """
    recorded = recorded or {}
    values = recorded.get("values")
    return {
        "state": recorded.get("state", "unknown"),
        "verification": recorded.get("verification", "not-checked"),
        "values": values,
        # Raw units, unlike `values`. Only the intervals are comparable between
        # the two, which is why the dialog names a mismatched change without a
        # number rather than putting a raw count beside a label reading °C.
        "actual": recorded.get("actual"),
        "at": recorded.get("at"),
        # False when nothing has been recorded at all: an unknown outcome
        # describes no settings, least of all these.
        "describes_desired": bool(values) and _same_settings(values, wanted or {}),
    }


def _same_settings(recorded: dict, wanted: dict) -> bool:
    """Whether two settings triples are the same request.

    ``reportable_change`` is a float that has been through JSON, so it is
    compared with a tolerance; the intervals are whole seconds and are not.
    """
    for field in ("min_interval", "max_interval"):
        if int(recorded.get(field, -1)) != int(wanted.get(field, -2)):
            return False
    return math.isclose(
        float(recorded.get("reportable_change", -1.0)),
        float(wanted.get("reportable_change", -2.0)),
        rel_tol=1e-9,
        abs_tol=1e-12,
    )


async def _settings_payload(db, device, info, row) -> dict:
    """Everything the settings dialog needs, in one answer.

    Reporting and polling share this endpoint because they are one dialog and
    one Save; two endpoints would mean two requests, the second of which can
    fail after the first has already succeeded.
    """
    from backend.app.services.zigbee.device_settings import resolve_poll_seconds, resolve_stale_after_seconds
    from backend.app.services.zigbee.reporting_targets import targets_for
    from backend.app.services.zigbee.sensors import PowerClass, power_class

    desired = await resolve_reporting(db, info)
    targets = targets_for(info)
    polled = power_class(device) is PowerClass.MAINS
    slowest = max((values.get("max_interval", 900) for values in desired.values()), default=900)
    applied = zigbee_coordinator.applied_reporting(info.ieee)

    return {
        "ieee": info.ieee,
        "kind": info.kind.value,
        "name": row.name if row else None,
        "adopted": await _is_adopted(db, info),
        "editable": {t.key: list(t.editable) for t in targets},
        # What the "change by" number is measured in. Sent rather than known by
        # the frontend: a key-to-unit map there would be a second copy of the
        # measurement registry, and it would drift at the first new quantity.
        "units": {t.key: t.unit for t in targets},
        "desired": desired,
        # Unknown rather than ok: after a restart nothing has been asked of this
        # device yet, and claiming a state nobody confirmed is the failure this
        # whole vocabulary exists to avoid.
        "applied": {t.key: _applied_entry(applied.get(t.key), desired.get(t.key)) for t in targets},
        "poll_seconds": await resolve_poll_seconds(db, info.ieee),
        "poll_supported": polled,
        "stale_after_seconds": await resolve_stale_after_seconds(db, info.ieee, polled=polled, max_interval=slowest),
    }


async def _load_for_settings(db, ieee: str):
    """The live device, its description and its row.

    Two different 404s on purpose: "never paired with us" and "not on the
    network right now" are different problems with different fixes, and one
    message covering both sends an operator to look in the wrong place.
    """
    from backend.app.services.zigbee.device_settings import load_device_row

    row = await load_device_row(db, ieee)
    if row is None:
        raise HTTPException(status_code=404, detail="This device has never paired with BamDude.")
    app = zigbee_coordinator.app
    device = _find_device(app, ieee) if app is not None else None
    if device is None:
        raise HTTPException(status_code=404, detail="This device is not on the network right now.")
    return device, describe_device(device), row


@router.get("/devices/{ieee}/settings")
async def get_device_settings(
    ieee: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SMART_PLUGS_READ),
):
    """What this device is asked to report, and what it made of the request."""
    device, info, row = await _load_for_settings(db, ieee)
    return await _settings_payload(db, device, info, row)


def _validate(update, info, resolved) -> None:
    """Refuse, out loud, anything that would be stored and never take effect.

    Every branch here is a setting that would otherwise sit in the API looking
    applied while the device runs something else -- the exact failure this
    cycle exists to remove.
    """
    from backend.app.services.zigbee.reporting_targets import targets_for

    by_key = {t.key: t for t in targets_for(info)}
    for key, values in (update.reporting or {}).items():
        target = by_key.get(key)
        if target is None:
            raise HTTPException(status_code=422, detail=f"This device has no '{key}' to report.")
        for field, value in values.model_dump(exclude_none=True).items():
            if field not in target.editable:
                raise HTTPException(status_code=422, detail=f"'{field}' cannot be set for '{key}' on this device.")
            resolved.setdefault(key, {})[field] = value

        # Checked against what RESOLVES, not only against what was sent: a
        # request moving one field at a time would otherwise walk past this.
        pair = resolved.get(key, {})
        if pair.get("min_interval", 0) > pair.get("max_interval", 0):
            raise HTTPException(
                status_code=422,
                detail=f"'{key}': the shortest gap between reports cannot be longer than the longest.",
            )


# What a request may spend waiting for a device. An awake plug answers in well
# under a second; a sleeper never answers at all, and four measurements times
# zigpy's own timeout is over a minute of a held connection. Six of those
# exhaust a browser's per-origin pool and freeze the tab -- measured on hardware
# once already, which is why no handler here waits on a device without a budget.
_APPLY_BUDGET_SECONDS = 5.0


async def _push_within_budget(coro, info, desired) -> None:
    """Re-issue the configuration, but do not hold the request for it.

    Shielded: the caller's budget expiring must not cancel the work. The device
    may well take the configuration a moment later, and cancelling would leave
    it half-applied for no gain. Whatever lands is recorded when it lands.

    Nothing is lost by giving up early. The desired state is already stored, so
    a device that was not reached is retried at its next contact -- which for a
    battery sensor is the normal path anyway.
    """
    task = asyncio.create_task(coro, name=f"zigbee-apply-{info.ieee}")

    def _record(finished: asyncio.Task) -> None:
        if finished.cancelled():
            return
        exc = finished.exception()
        if exc is not None:
            logger.warning("Zigbee %s: applying settings failed: %s", info.ieee, exc)
            return
        from backend.app.services.zigbee.reporting_apply import fully_applied

        applied = finished.result()
        zigbee_coordinator.record_reporting(info.ieee, desired if fully_applied(applied) else {}, applied)

    task.add_done_callback(_record)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), _APPLY_BUDGET_SECONDS)


async def _reissue(db, device, info) -> None:
    """Re-issue this device's resolved configuration, within the budget.

    Reporting parameters live IN the device, so saving alone changes nothing
    until ``configure_reporting`` runs again. Both classes go through here: a
    plug is awake and takes it immediately, a sleeper answers nothing and is
    retried at its next contact.

    Shared by saving and by clearing. Clearing used only to save, so a reset
    showed farm defaults in the answer while the device went on running the old
    values -- the same setting-that-did-nothing this cycle exists to remove,
    arriving through the one path it had not covered.
    """
    from backend.app.services.zigbee.reporting import bind_sensor, push_plug_reporting

    desired = await resolve_reporting(db, info)
    if info.kind is DeviceKind.SENSOR:
        push = bind_sensor(device, info.ieee, desired)
    else:
        plug = (
            await db.execute(select(SmartPlug).where(func.lower(SmartPlug.zigbee_ieee) == info.ieee.lower()))
        ).scalar_one_or_none()
        push = push_plug_reporting(device, info, desired, plug_id=plug.id if plug else None)

    await _push_within_budget(push, info, desired)


@router.put("/devices/{ieee}/settings")
async def update_device_settings(
    ieee: str,
    update: DeviceSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SMART_PLUGS_UPDATE),
):
    """Store what the operator chose, and apply it if the device is awake.

    A sleeping device is a **success**: the desired state is saved and applied
    at its next contact. Answering with an error there would report the
    ordinary course of events for a battery sensor as a broken feature.
    """
    from backend.app.services.zigbee.device_settings import save_overrides
    from backend.app.services.zigbee.sensors import PowerClass, power_class

    device, info, row = await _load_for_settings(db, ieee)

    if update.poll_seconds is not None and power_class(device) is not PowerClass.MAINS:
        raise HTTPException(
            status_code=422,
            detail=(
                "This device sleeps between reports, so it cannot be polled. "
                "It reports on its own instead -- change how often it reports."
            ),
        )

    resolved = await resolve_reporting(db, info)
    _validate(update, info, resolved)

    stored = dict(row.reporting or {})
    for key, values in (update.reporting or {}).items():
        stored.setdefault(key, {}).update(values.model_dump(exclude_none=True))
    await save_overrides(
        db,
        info.ieee,
        reporting=stored,
        poll_seconds=update.poll_seconds,
        stale_after_seconds=update.stale_after_seconds,
    )

    await _reissue(db, device, info)

    device, info, row = await _load_for_settings(db, ieee)
    return await _settings_payload(db, device, info, row)


@router.delete("/devices/{ieee}/settings")
async def clear_device_settings(
    ieee: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SMART_PLUGS_UPDATE),
):
    """Drop this device's overrides so the farm defaults apply again."""
    from backend.app.services.zigbee.device_settings import save_overrides

    device, info, row = await _load_for_settings(db, ieee)
    await save_overrides(db, info.ieee, reporting={}, poll_seconds=0, stale_after_seconds=0)
    await _reissue(db, device, info)
    device, info, row = await _load_for_settings(db, ieee)
    return await _settings_payload(db, device, info, row)


# A leave request the device must be awake to receive. Beyond this we stop
# waiting and drop it locally: a switched-off plug or a sleeping sensor would
# otherwise hold the request for as long as zigpy cares to retry.
_REMOVE_BUDGET_SECONDS = 10.0


@router.delete("/devices/{ieee}")
async def remove_device(
    ieee: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SMART_PLUGS_DELETE),
):
    """Remove a device from the network, and say which of the two happened.

    ``left`` means the device acknowledged and is off the network. ``forced``
    means it never answered — it keeps the network key and will try to rejoin
    when it is powered back on, so it should be reset at the device if that is
    not wanted. For a battery sensor ``forced`` is the normal outcome, since it
    is asleep almost all of the time.

    Anything BamDude held about the device goes with it. Leaving a ``SmartPlug``
    row behind gives a card bound to a device that is no longer on the network:
    unreachable for ever, with nothing on screen saying why.
    """
    from backend.app.services.zigbee.device_settings import forget_device_row
    from backend.app.services.zigbee.driver import zigbee_smart_plug_service
    from backend.app.services.zigbee.reporting import forget_sensor_listeners
    from backend.app.services.zigbee.sensors import sensor_store

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

    outcome = "left"
    try:
        await asyncio.wait_for(app.remove(match.ieee), timeout=_REMOVE_BUDGET_SECONDS)
    except TimeoutError:
        outcome = "forced"
        logger.info("Zigbee device %s did not answer the leave request — removed locally", ieee)

    sensor_store.forget(str(match.ieee))
    zigbee_coordinator.forget_reporting(str(match.ieee))
    # The attachment record too: pairing this address again hands back new
    # cluster objects, and a stale record would leave those unheard.
    forget_sensor_listeners(str(match.ieee))

    # The row goes with the device, in the same call. Its hourly energy
    # snapshots go too (ON DELETE CASCADE); per-print energy is unaffected,
    # being written onto the archive rather than looked up.
    deleted_plug_id = None
    plug = (
        await db.execute(select(SmartPlug).where(func.lower(SmartPlug.zigbee_ieee) == str(match.ieee).lower()))
    ).scalar_one_or_none()
    if plug is not None:
        deleted_plug_id = plug.id
        await zigbee_smart_plug_service.teardown(plug.id)
        await db.delete(plug)
        await db.commit()

    # And what the radio knew about it, with the adopted sensor attached to it.
    # Left behind, the settings would be re-applied to whatever device is next
    # given this address — which is a different device wearing an old
    # configuration nobody chose for it.
    await forget_device_row(db, str(match.ieee))

    logger.info("Removed Zigbee device %s from the network (%s)", ieee, outcome)
    return {"removed": str(match.ieee), "outcome": outcome, "deleted_plug_id": deleted_plug_id}


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
