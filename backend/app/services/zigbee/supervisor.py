"""Brings the radio back after it goes away, and is the one place a restart is spelled out.

⚠️ **Why this exists.** ``ZigbeeCoordinator.connection_lost`` set the status to
``error`` and stopped there — liveness was deferred when the coordinator was
first written and never came back. So a dongle unplugged for thirty seconds
left every Zigbee plug dead until the whole application was restarted, and the
UI, which does receive the status change, had nothing further to receive
because nothing on the server ever changed it back. Reported from a farm: the
dongle was moved to another room, plugged back in, and nothing happened.

⚠️ **A restart is not ``stop()`` then ``start()``.** Every cached cluster
listener belongs to a cluster object ``stop()`` orphans, and reporting has to be
re-established afterwards or the radio comes up looking healthy while doing half
its job — commands reach the cluster and work, so plugs switch, but nothing
feeds the cache and status stays "unreachable" forever. That full sequence lived
only inside the ``POST /zigbee/restart`` route; it is here now so the route and
the supervisor cannot drift, because the drift would be silent in exactly that
way.

⚠️ **Deliberately driven by status, not by the ``connection_lost`` callback.**
Reacting to the callback would only ever cover a radio that was up and then
died. Reading ``status.state`` covers that *and* a dongle absent at boot, a port
held by Zigbee2MQTT that is later freed, and a path corrected in Settings — all
of which are the same question ("is it down, and can it come up now?") and none
of which the callback ever fires for.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.websocket import ws_manager
from backend.app.models.settings import Settings
from backend.app.models.smart_plug import SmartPlug
from backend.app.services.zigbee.coordinator import CoordinatorState, zigbee_coordinator

logger = logging.getLogger(__name__)

SETTING_KEYS = ("zigbee_enabled", "zigbee_transport", "zigbee_path")

#: One restart at a time, shared by the route and the supervisor. The radio lock
#: is a *file* lock and only distinguishes processes, so it cannot see an
#: operator pressing Restart while a retry is already in flight.
restart_lock = asyncio.Lock()

#: How often the supervisor looks at the status. Cheap — an attribute read.
_TICK_SECONDS = 15.0
#: Wait between retries, doubling. A dongle being carried to another room is
#: back within a minute; a port held by another program may never free, and
#: hammering it neither helps nor is free on a Pi.
_MIN_BACKOFF_SECONDS = 15.0
_MAX_BACKOFF_SECONDS = 300.0


async def read_settings(db: AsyncSession) -> dict[str, str]:
    """The three settings ``start`` needs, read fresh.

    ⚠️ Re-read on every attempt rather than remembered from boot. On Windows a
    dongle can come back on a different COM port, and the operator's fix is to
    correct ``zigbee_path`` in Settings — which a cached copy would ignore until
    the restart this exists to avoid.
    """
    rows = (await db.execute(select(Settings.key, Settings.value).where(Settings.key.in_(SETTING_KEYS)))).all()
    found = {key: (value or "") for key, value in rows}
    return {key: found.get(key, "") for key in SETTING_KEYS}


async def restart_radio(db: AsyncSession) -> None:
    """Stop the coordinator, clear what it orphaned, start it, re-arm reporting.

    ⚠️ The order is the contract, not a preference. Listeners are cleared
    *after* the stop that orphaned their clusters and *before* the start that
    creates new ones; reporting is re-established last, once there is a radio to
    talk through. A read still in flight holds those same orphaned clusters, so
    it is cancelled with them.

    Caller holds :data:`restart_lock`.
    """
    from backend.app.services.zigbee import reporting
    from backend.app.services.zigbee.driver import zigbee_smart_plug_service

    settings = await read_settings(db)

    await zigbee_coordinator.stop()
    await zigbee_smart_plug_service.cancel_refreshes()
    zigbee_smart_plug_service._listeners.clear()
    await zigbee_coordinator.start(settings)

    if zigbee_coordinator.app is None:
        return

    rows = (await db.execute(select(SmartPlug).where(SmartPlug.plug_type == "zigbee"))).scalars().all()
    if rows:
        # Resolved through the module so a test can patch it, and so the import
        # cannot go stale against a reload.
        wired = await reporting.subscribe_all(zigbee_smart_plug_service, rows)
        logger.info("Zigbee reporting re-established for %s/%s plug(s)", wired, len(rows))


class RadioSupervisor:
    """Watches the coordinator's status and retries while it is down."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    def start(self, session_factory) -> None:
        """Begin watching. Idempotent — a second call is ignored.

        ⚠️ Started whether or not the radio came up, unlike the poller. A
        supervisor that only runs when the radio is already healthy cannot
        recover the case it exists for.
        """
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(session_factory), name="zigbee_supervisor")

    async def stop(self) -> None:
        """Cancel and await, so teardown does not race a retry."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self, session_factory) -> None:
        backoff = _MIN_BACKOFF_SECONDS
        next_attempt = 0.0
        last_reason = ""

        while True:
            await asyncio.sleep(_TICK_SECONDS)
            try:
                state = zigbee_coordinator.status.state
                if state is not CoordinatorState.ERROR:
                    # Includes ``disabled``: an install that does not want
                    # Zigbee is correctly configured, not broken.
                    backoff = _MIN_BACKOFF_SECONDS
                    next_attempt = 0.0
                    last_reason = ""
                    continue

                now = time.monotonic()
                if now < next_attempt:
                    continue

                async with restart_lock, session_factory() as db:
                    await restart_radio(db)

                status = zigbee_coordinator.status
                if status.state is CoordinatorState.UP:
                    logger.info("Zigbee radio is back")
                    backoff = _MIN_BACKOFF_SECONDS
                    next_attempt = 0.0
                    last_reason = ""
                    # ⚠️ The supervisor has no HTTP response to carry the news,
                    # so it says so itself. Without this an operator watching
                    # the page sees the radio-down badge until something else
                    # happens to invalidate the query.
                    await ws_manager.broadcast(
                        {
                            "type": "zigbee_status_changed",
                            "state": status.state.value,
                            "reason": status.reason,
                        }
                    )
                    continue

                backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)
                next_attempt = time.monotonic() + backoff
                # ⚠️ Logged on change only. A port held by another program, or
                # a path that is simply wrong, would otherwise write the same
                # warning every five minutes for as long as the install runs.
                if status.reason != last_reason:
                    last_reason = status.reason
                    logger.warning(
                        "Zigbee radio still down, retrying in %.0fs: %s", backoff, status.reason or "no reason given"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a bad cycle must not end the loop
                logger.warning("Zigbee supervisor cycle failed: %s", exc)
                backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)
                next_attempt = time.monotonic() + backoff


zigbee_supervisor = RadioSupervisor()
