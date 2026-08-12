"""Rebuild MQTT sessions that stopped reconnecting on their own (upstream #2732).

A printer lost its session to a keep-alive timeout at 02:19 and did not come
back until 11:24 — **nine hours offline with the web UI open the whole time**.

``BambuMQTTClient.check_staleness()`` was never going to catch it. Its first line
is ``if self.state.connected and self.is_stale()``, so it only handles the
half-broken session that is *still connected but has gone quiet*. That client had
``connected=False`` from 02:19 onward, so every call returned immediately and
paho's own retry was the only thing left watching. When that stopped making
progress, nothing noticed.

Ours has the same first line, and — unlike upstream — ``check_staleness`` is not
even on a timer here: it is called from ``PrinterManager.get_status()``, i.e.
reactively, when something asks. So a printer nobody is looking at has nothing
watching it at all.

**Four conditions, and the fourth is what makes this safe to run.** A rebuild
happens only when the client is disconnected, *had* a working session before, has
been silent past the grace period, **and its MQTT port still answers**. Without
that last one a farm powered down overnight would churn clients and spam the log
every minute for every machine; with it, a switched-off printer is simply left to
paho, which is the right owner for that case.

The grace period sits well past both the 60 s staleness timeout and the 30 s
maximum reconnect backoff, so a session recovering on its own is never
interrupted.

The rebuild itself needs no new machinery: ``PrinterManager.connect_printer``
already tears down the old client and constructs a fresh one, so paho's QoS-1
queue goes with it and a ``project_file`` left unacked on the dead session cannot
replay into the new one (the #1136 failure mode). Print-lifecycle flags are
carried across explicitly by ``carry_print_lifecycle_from``, so a print that
finished during the dead window is still recognised.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select

from backend.app.core.database import async_session
from backend.app.models.printer import Printer
from backend.app.services.printer_diagnostic import PORT_MQTT, check_port
from backend.app.services.printer_manager import printer_manager

logger = logging.getLogger(__name__)

#: How often the sweep runs.
SWEEP_INTERVAL_SECONDS = 60.0

#: How long a disconnected client must have been silent before we rebuild it.
#: Past the 60 s staleness timeout and the 30 s max reconnect backoff, so a
#: session that is recovering by itself is never cut short.
SILENCE_GRACE_SECONDS = 300.0

#: Minimum gap between two rebuilds of the same printer. A printer whose session
#: dies immediately must not be rebuilt every sweep.
REBUILD_COOLDOWN_SECONDS = 600.0

_last_rebuild: dict[int, float] = {}
_task: asyncio.Task | None = None


def _silence_seconds(client) -> float | None:
    """Seconds since this client last heard anything, or ``None`` if it never did.

    ``_last_message_time`` is 0 until the first message arrives, which is exactly
    the "never had a working session" case — a printer that has never been
    reachable is not something to rebuild, it is something to configure.
    """
    last = getattr(client, "_last_message_time", 0) or 0
    if not last:
        return None
    return time.time() - last


async def should_rebuild(printer: Printer, client, *, now: float | None = None) -> bool:
    """True when this printer's session is dead and worth rebuilding.

    Separated from the sweep so the decision can be tested without a loop, a
    database or a socket.
    """
    if client is None or client.state.connected:
        return False

    silence = _silence_seconds(client)
    if silence is None or silence < SILENCE_GRACE_SECONDS:
        return False

    now = time.time() if now is None else now
    last_rebuild = _last_rebuild.get(printer.id)
    if last_rebuild is not None and now - last_rebuild < REBUILD_COOLDOWN_SECONDS:
        return False

    if not printer.ip_address:
        return False

    # Last, because it is the only condition that costs a network round trip.
    return await check_port(printer.ip_address, PORT_MQTT)


async def sweep_once() -> int:
    """One pass over the printers. Returns how many sessions were rebuilt."""
    rebuilt = 0
    async with async_session() as db:
        result = await db.execute(select(Printer).where(Printer.is_active.is_(True)).where(Printer.archived.is_(False)))
        printers = list(result.scalars().all())

    for printer in printers:
        client = printer_manager.get_client(printer.id)
        try:
            if not await should_rebuild(printer, client):
                # A printer that came back clears its cooldown, so the next
                # failure is treated on its own merits rather than being
                # suppressed by an old one.
                if client is not None and client.state.connected:
                    _last_rebuild.pop(printer.id, None)
                continue

            silence = _silence_seconds(client)
            logger.warning(
                "Rebuilding MQTT session for %s (id=%s, ip=%s): disconnected and silent for %.0fs, "
                "but port %s still answers. Last connect error: %s",
                printer.name,
                printer.id,
                printer.ip_address,
                silence or 0,
                PORT_MQTT,
                getattr(client, "_last_connect_error", None) or "none recorded",
            )
            _last_rebuild[printer.id] = time.time()
            if await printer_manager.connect_printer(printer):
                rebuilt += 1
            else:
                logger.warning("MQTT session rebuild failed for %s (id=%s)", printer.name, printer.id)
        except Exception as exc:  # noqa: BLE001 — one bad client must not end the sweep
            logger.warning("Connection watchdog failed for printer %s: %s", printer.id, exc)

    return rebuilt


async def _run() -> None:
    while True:
        try:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            await sweep_once()
        except asyncio.CancelledError:
            break
        except Exception as exc:  # noqa: BLE001 — the sweep must outlive its own failures
            logger.warning("Connection watchdog sweep failed: %s", exc)


def start_connection_watchdog() -> None:
    """Start the sweep. Idempotent."""
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_run())
        logger.info(
            "Connection watchdog started (every %.0fs, grace %.0fs)",
            SWEEP_INTERVAL_SECONDS,
            SILENCE_GRACE_SECONDS,
        )


async def stop_connection_watchdog() -> None:
    """Cancel the sweep and wait for it to unwind."""
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
