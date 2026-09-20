"""Chamber light on the camera's behalf: a per-printer lease.

Every path that takes a frame from a printer's camera — a Telegram photo, the
snapshot route, a browser stream, the Camera Wall, the finish photo, the plate
check, Obico — asks this module for the light first and gives it back after.
The module switches the light on only when it is OFF, remembers that it did,
and switches it off again only in that case, once nobody needs it and a grace
window has passed. Whoever already had the light on notices nothing; whoever
has the feature off (the farm default) notices nothing either.

Ownership is the whole safety story. A ``lights_report`` arrives for every
switch from anywhere — the card button, the printer's screen, Bambu Studio,
the firmware at print start or end. A change we did not command takes the
ownership away: the operator switched the light off during a stream, so we do
not switch it back on; they switched it on before the stream, so we do not
switch it off after. A change we DID command — same direction, within a short
window after our own ``ledctrl`` — is the confirmation ``Lease.settle`` waits
for before a one-shot capture: the firmware answers ``ledctrl`` with nothing of
its own, the next status push is the acknowledgement.

The lease is refcounted per printer, so two browser tabs are one "on" and one
"off". A one-shot use keeps the light for a grace window after release, so two
photos in a row do not blink. A poller (the Camera Wall in snapshot mode, the
embedded viewer) declares its cadence with the frame request and the lease is
held for that cadence plus the capture timeout: while the wall is open the
light stays on, and once it is closed the light goes off within that hold.

Lives in the main process — the MQTT client that speaks ``ledctrl`` is here
and the camera worker never sees it. Nothing here persists: a restart with a
lease open leaves the light on, and the next lease finds it on and does not
take ownership of it. That is the right side to err on.

Vault: 60-specs/camera-light-lease-spec, 40-invariants/inv-camera-light-lease.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: How long the light stays on after the last one-shot lease is released.
GRACE_SECONDS = 10.0
#: How long ``Lease.settle`` waits for the printer to report the light on.
CONFIRM_TIMEOUT_SECONDS = 3.0
#: A ``lights_report`` in the direction we commanded within this window is ours.
ATTRIBUTION_WINDOW_SECONDS = 5.0
#: The longest cadence a poller may declare (``?poll=`` on the snapshot route).
MAX_POLL_MS = 120_000
#: Added to a declared cadence: covers the capture timeout (15 s) plus slack.
POLL_HOLD_MARGIN_SECONDS = 20.0

#: ``printers.camera_light_auto``: ``off`` excludes the printer; ``inherit`` (and the
#: no-longer-offered ``on``) defer to the farm toggle, which is the master switch.
POLICY_VALUES: tuple[str, ...] = ("inherit", "on", "off")
FARM_SETTING = "camera_light_auto"
OBICO_SETTING = "camera_light_auto_obico"
#: Purposes that never take the light, whatever the settings say.
NEVER_PURPOSES = frozenset({"layer_timelapse"})
#: Purposes that ALWAYS take the light, whatever the settings say. The plate
#: check compares the camera view against a reference calibrated with the
#: light on; a check in the dark would differ from it and pause a print for
#: nothing. It lit the plate before this module existed (main.py and the
#: card each did it by hand) and keeps doing so — only through the lease now.
ALWAYS_PURPOSES = frozenset({"plate_check"})


@dataclass
class _PrinterLight:
    refcount: int = 0
    #: True while the light is on because WE switched it on.
    owned: bool = False
    #: The latest declared hold, absolute monotonic time.
    hold_until: float = 0.0
    #: Set when the printer reports the light on after our own "on".
    confirmed: asyncio.Event | None = None
    last_command_at: float = 0.0
    last_command_on: bool | None = None
    release_task: asyncio.Task | None = None


@dataclass(frozen=True)
class Lease:
    printer_id: int
    purpose: str
    _light: _PrinterLight = field(repr=False)

    async def settle(self) -> None:
        """Wait for the printer to confirm OUR "on" before a one-shot capture.

        Returns at once when the light was already on, when we did not switch
        it, or when the confirmation has already arrived. A printer that stays
        silent for ``CONFIRM_TIMEOUT_SECONDS`` does not fail the capture — the
        frame is taken as it comes.
        """
        light = self._light
        if not light.owned or light.confirmed is None or light.confirmed.is_set():
            return
        try:
            await asyncio.wait_for(light.confirmed.wait(), CONFIRM_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.info(
                "camera light: printer %s did not confirm the light within %.0fs; capturing anyway",
                self.printer_id,
                CONFIRM_TIMEOUT_SECONDS,
            )


_lights: dict[int, _PrinterLight] = {}
#: Replaced by tests that drive the grace and hold windows.
_clock = time.monotonic
_sleep = asyncio.sleep


def hold_for_poll(poll_ms: int | None) -> float | None:
    """The hold a poller earns by declaring its cadence, in seconds."""
    if poll_ms is None:
        return None
    poll_ms = max(0, min(int(poll_ms), MAX_POLL_MS))
    return poll_ms / 1000 + POLL_HOLD_MARGIN_SECONDS


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() == "true"


def _get_client(printer_id: int):
    from backend.app.services.printer_manager import printer_manager

    return printer_manager.get_client(printer_id)


async def allowed(printer_id: int, purpose: str) -> bool:
    """The settings' answer for this printer and this use of the camera.

    Read on every call, never cached: flipping the toggle must act at once.
    The farm toggle is the master switch — off, and no printer takes the
    light whatever its own row says; on, and a printer may still say ``off``
    for itself. The Obico sub-toggle is the farm's alone.
    """
    if purpose in NEVER_PURPOSES:
        return False
    if purpose in ALWAYS_PURPOSES:
        return True
    from sqlalchemy import select

    from backend.app.api.routes.settings import get_setting
    from backend.app.core import database
    from backend.app.models.printer import Printer

    async with database.async_session() as db:
        if not _truthy(await get_setting(db, FARM_SETTING)):
            return False
        policy = (
            await db.execute(select(Printer.camera_light_auto).where(Printer.id == printer_id))
        ).scalar_one_or_none()
        if policy is None or policy == "off":
            return False
        if purpose == "obico" and not _truthy(await get_setting(db, OBICO_SETTING)):
            return False
    return True


def _switch(client, light: _PrinterLight, on: bool) -> bool:
    """Send ``ledctrl``; a refusal or an exception never reaches the caller."""
    try:
        sent = bool(client.set_chamber_light(on))
    except Exception as e:  # noqa: BLE001 — the capture must not fail on the light
        logger.warning("camera light: switching the light %s failed: %s", "on" if on else "off", e)
        sent = False
    if not sent:
        return False
    light.last_command_at = _clock()
    light.last_command_on = on
    if on:
        light.owned = True
        light.confirmed = asyncio.Event()
    else:
        light.owned = False
        light.confirmed = None
    return True


async def acquire(printer_id: int | None, purpose: str, *, hold: float | None = None) -> Lease | None:
    """Take the light for one use of the camera. ``None`` means: nothing to do.

    Nothing happens when the feature is off for this printer and purpose,
    when the printer has no client or is not connected, or when it has never
    reported a controllable chamber light. The first holder switches the light
    on if it is off; every holder after that only counts.
    """
    if printer_id is None:
        return None
    try:
        if not await allowed(printer_id, purpose):
            return None
    except Exception as e:  # noqa: BLE001 — a settings read must not fail a capture
        logger.warning("camera light: could not read the settings for printer %s: %s", printer_id, e)
        return None
    client = _get_client(printer_id)
    state = getattr(client, "state", None)
    if client is None or state is None or not state.connected or not getattr(state, "has_chamber_light", False):
        return None

    light = _lights.setdefault(printer_id, _PrinterLight())
    if light.release_task is not None and not light.release_task.done():
        light.release_task.cancel()
    light.release_task = None
    light.refcount += 1
    if hold:
        light.hold_until = max(light.hold_until, _clock() + hold)
    if light.refcount == 1 and not light.owned and not state.chamber_light:
        if _switch(client, light, True):
            logger.info("camera light: switched on for printer %s (%s)", printer_id, purpose)
    return Lease(printer_id, purpose, light)


def release(lease: Lease | None) -> None:
    """Give the light back. The last holder arms the switch-off, after the grace or the hold."""
    if lease is None:
        return
    light = lease._light
    light.refcount = max(0, light.refcount - 1)
    if light.refcount > 0 or not light.owned:
        return
    delay = max(GRACE_SECONDS, light.hold_until - _clock())
    light.release_task = asyncio.get_running_loop().create_task(
        _release_later(lease.printer_id, light, delay), name=f"camera-light-off-{lease.printer_id}"
    )


async def _release_later(printer_id: int, light: _PrinterLight, delay: float) -> None:
    await _sleep(delay)
    if light.refcount > 0 or not light.owned:
        return
    client = _get_client(printer_id)
    if client is None:
        light.owned = False
        return
    if _switch(client, light, False):
        logger.info("camera light: switched off for printer %s", printer_id)


async def note_light_report(printer_id: int, on: bool) -> None:
    """The printer reported a switch. Ours confirms a pending "on"; anyone else's ends our ownership."""
    light = _lights.get(printer_id)
    if light is None:
        return
    ours = light.last_command_on is on and (_clock() - light.last_command_at) <= ATTRIBUTION_WINDOW_SECONDS
    if ours:
        if on and light.confirmed is not None:
            light.confirmed.set()
        return
    if light.owned:
        logger.info(
            "camera light: printer %s light switched %s by someone else; no longer ours to switch off",
            printer_id,
            "on" if on else "off",
        )
        light.owned = False
    if light.confirmed is not None:
        # Whatever the light is now, it is not going to become "our on" any more.
        light.confirmed.set()


@contextlib.asynccontextmanager
async def held(printer_id: int | None, purpose: str, *, hold: float | None = None) -> AsyncIterator[Lease | None]:
    lease = await acquire(printer_id, purpose, hold=hold)
    try:
        yield lease
    finally:
        release(lease)


async def shutdown() -> None:
    """Drop every pending switch-off. The lights stay as they are (see the module docstring)."""
    tasks = [light.release_task for light in _lights.values() if light.release_task and not light.release_task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _lights.clear()
