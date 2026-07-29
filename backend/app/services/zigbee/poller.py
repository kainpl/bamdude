"""Periodic reads that keep the plug cache honest.

Attribute reporting is set up for every plug (see ``reporting.py``) and is
welcome when it works. This module exists because it frequently does not, and
because the failure is invisible: a subscription that silently never fires looks
exactly like a plug whose readings have not changed.

The cadence is ZHA's. Its ``ElectricalMeasurementPoller`` reads the cluster
every 30–45 s for **every** device, and a sibling class exempts precisely four
models known to report reliably — the plug this was built against is not among
them. When the reference implementation for this stack polls by default and
allowlists the exceptions, "reporting is enough" is not a defensible position.

The interval is randomised per cycle for the reason ZHA randomises it: a farm's
worth of plugs polled in lockstep is a burst of mesh traffic every N seconds
instead of a trickle.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random

from sqlalchemy import select

from backend.app.models.smart_plug import SmartPlug

logger = logging.getLogger(__name__)

# ZHA's AggregatedClusterPoller._REFRESH_INTERVAL.
_POLL_INTERVAL_SECONDS = (30, 45)


class ZigbeePoller:
    """One background task reading every Zigbee plug on a timer."""

    def __init__(self):
        self._task: asyncio.Task | None = None

    def start(self, service, session_factory) -> None:
        """Begin polling. Idempotent — a second call is ignored."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(service, session_factory), name="zigbee_poller")

    async def stop(self) -> None:
        """Cancel and await the task, so shutdown does not race a read."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self, service, session_factory) -> None:
        while True:
            await asyncio.sleep(random.randint(*_POLL_INTERVAL_SECONDS))
            try:
                await self._poll_once(service, session_factory)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a bad cycle must not end the loop
                # Losing the loop would take readings back to "only updates on
                # restart", which is the exact symptom this was written to fix.
                logger.warning("Zigbee poll cycle failed: %s", exc)

    async def _poll_once(self, service, session_factory) -> None:
        """Read every configured Zigbee plug.

        The plug list is re-queried each cycle rather than captured at startup,
        so a plug added afterwards joins the rotation without a restart.
        """
        async with session_factory() as db:
            plugs = (await db.execute(select(SmartPlug).where(SmartPlug.plug_type == "zigbee"))).scalars().all()
        for plug in plugs:
            try:
                await service.refresh(plug)
            except Exception as exc:  # noqa: BLE001 — one plug must not cost the rest their poll
                logger.debug("Zigbee plug %s: poll failed: %s", plug.id, exc)


zigbee_poller = ZigbeePoller()
