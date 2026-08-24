"""Cloud Link service — the one thing that turns the link on and off.

Everything else in ``cloud_link`` is a part: the store keeps the pairing, the
uplink turns broadcasts into frames, the client owns a socket. This module owns
their *lifecycle*, and it is the only place that knows the whole set. The
settings routes and the application lifespan both come here; neither of them
constructs a client, and neither of them touches the broadcast listener.

Four rulings shape it.

**Off is the default, and three separate states mean off.** ``start()`` returns
having done nothing when the link is disabled, when the farm is not paired, or
when the portal has revoked it. They are kept apart because their repairs are
different — flip a switch, pair, pair again — and a single "not ready" flag
would send half the users down the wrong one. Cloud Link ships disabled, so
"start it and see whether it connects" would open a socket nobody asked for.

**``stop()`` cancels the task; it does not merely ask it to end.**
``CloudLinkClient.run`` honours its stop event, but only at the next point it
looks — and during a handshake that can be up to ~35 s of connect and handshake
timeouts. Docker's and systemd's default grace periods are shorter than that,
so a stop that only set the event would be a SIGKILL in production and a hung
test suite in development. The event is set as well, so anything already
waiting on it sees a stop rather than a cancellation.

**Every pair is closed in one place.** A registered ``uplink.feed`` runs ahead
of every browser write in the product, so one left behind after the link is
gone is a queue that fills forever for nothing. It is unregistered by the task
that owns it, in a ``finally`` — which covers the loop ending *by itself*, the
case ``stop()`` would never hear about — and again by ``stop()``, because a
task cancelled before its first step never reaches its own ``finally``.
Unregistering twice is deliberately harmless.

**The writer prunes.** The audit table is bounded by time and nothing else
bounds it, so the agent that writes the rows is the thing that must delete
them. The tick is a child of the client task rather than a service-wide task:
it then cannot outlive the link that justifies it, and there is one cancel to
get right instead of two.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.services.cloud_link.client import CloudLinkClient
from backend.app.services.cloud_link.store import get_config, get_publish_set, prune_audit
from backend.app.services.cloud_link.uplink import Uplink

logger = logging.getLogger(__name__)

#: How often the audit table is swept. Daily: the retention window is 30 days,
#: so the table is never more than one day past it, and a sweep costs one
#: indexed DELETE.
AUDIT_PRUNE_INTERVAL_S = 24 * 60 * 60.0

#: How long after the link starts the first sweep runs. Not immediately —
#: startup is the busiest minute a BamDude instance has, and a month-old audit
#: row can wait sixty seconds longer.
AUDIT_PRUNE_FIRST_DELAY_S = 60.0

#: The name the client task carries. Read by ``asyncio.all_tasks()`` in a leak
#: check and in the tests; the prune tick extends it so both are recognisable.
TASK_NAME = "cloud-link"
PRUNE_TASK_NAME = "cloud-link-audit-prune"


class CloudLinkService:
    """Owns the running link: one uplink, one client, one task.

    A class rather than module functions because the state is a set that has to
    move together — an uplink whose listener is registered, the client draining
    it, and the task running that client are meaningless apart. The module
    exposes a single instance, :data:`cloud_link_service`; the constructor
    arguments exist so a test can supply its own database and its own
    ``ConnectionManager`` instead of reaching for the process-wide ones.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        connections: Any | None = None,
        prune_interval_s: float = AUDIT_PRUNE_INTERVAL_S,
        prune_first_delay_s: float = AUDIT_PRUNE_FIRST_DELAY_S,
    ):
        """
        Args:
            session_factory: Opens a database session. ``None`` resolves
                ``core.database.async_session`` **at each use** — a restore
                rebinds that name, and a factory captured at import would go on
                handing out sessions on an engine that has been disposed.
            connections: The broadcast hub the uplink listens to. ``None``
                resolves the ``ws_manager`` singleton lazily, so importing this
                module does not drag the WebSocket stack in behind it.
            prune_interval_s: Seconds between audit sweeps.
            prune_first_delay_s: Seconds before the first one.
        """
        self._session_factory = session_factory
        self._connections = connections
        self._prune_interval_s = prune_interval_s
        self._prune_first_delay_s = prune_first_delay_s

        self._task: asyncio.Task | None = None
        self._client: CloudLinkClient | None = None
        self._uplink: Uplink | None = None
        self._stop_event: asyncio.Event | None = None
        # The hub the running link actually registered with, remembered so
        # ``stop()`` unregisters from the same object even if the default moved.
        self._registered_with: Any | None = None
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------ the plumbing

    def _sessions(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is not None:
            return self._session_factory
        from backend.app.core import database

        return database.async_session

    def _hub(self) -> Any:
        if self._connections is not None:
            return self._connections
        from backend.app.core.websocket import ws_manager

        return ws_manager

    def _guard(self) -> asyncio.Lock:
        """The start/stop mutex, rebuilt if the event loop changed.

        ``start()`` reads the pairing row before it spawns anything, so two
        concurrent calls — the lifespan and a settings route saving at the same
        moment — could both pass the "already running" check and leave two
        links behind. A plain ``asyncio.Lock`` created once would be the right
        fix in production and a ``RuntimeError`` in the test suite, where this
        singleton outlives the per-test event loop it was first used on.
        """
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    @property
    def running(self) -> bool:
        """Whether a client task is live. A task that ended by itself is not."""
        return self._task is not None and not self._task.done()

    # --------------------------------------------------------------- the start

    async def start(self) -> None:
        """Bring the link up, unless this farm has not asked for one.

        Idempotent: a second call while a link is running does nothing. The
        pairing row is read once, up front, and the three reasons not to start
        are logged apart so the log says which repair is needed.
        """
        async with self._guard():
            if self.running:
                logger.debug("Cloud Link: already running")
                return

            sessions = self._sessions()
            async with sessions() as session:
                config = await get_config(session)
                enabled = bool(config.enabled)
                paired = bool(config.instance_id)
                revoked = bool(config.revoked)
                published = await get_publish_set(session) if enabled and paired and not revoked else set()

            if not enabled:
                logger.debug("Cloud Link: disabled — not connecting")
                return
            if not paired:
                logger.info("Cloud Link: enabled but not paired — pair this instance to connect")
                return
            if revoked:
                logger.info("Cloud Link: revoked by the portal — pair this instance again to connect")
                return

            uplink = Uplink()
            # The raw allowlist, as the initial filter. ``build_snapshot``
            # replaces it with the availability-filtered set at the first
            # successful hello — that is the set ``drain`` must end up with,
            # and only a session can tell which of these printers are active.
            uplink.set_publish_set(published)
            client = CloudLinkClient(session_factory=sessions, uplink=uplink)
            hub = self._hub()
            hub.add_internal_listener(uplink.feed)

            stop_event = asyncio.Event()
            self._uplink = uplink
            self._client = client
            self._stop_event = stop_event
            self._registered_with = hub
            self._task = asyncio.create_task(self._run(client, uplink, stop_event, hub), name=TASK_NAME)
            logger.info("Cloud Link: started (%d printer(s) published)", len(published))

    async def _run(
        self,
        client: CloudLinkClient,
        uplink: Uplink,
        stop_event: asyncio.Event,
        hub: Any,
    ) -> None:
        """The client loop, plus the audit tick, plus the cleanup neither owns.

        The ``finally`` is the reason this wrapper exists: the client loop ends
        on its own in cases nobody calls ``stop()`` for — an unpaired farm, a
        revocation — and the listener has to go with it.
        """
        prune = asyncio.create_task(self._prune_loop(), name=PRUNE_TASK_NAME)
        try:
            await client.run(stop_event)
        finally:
            prune.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await prune
            hub.remove_internal_listener(uplink.feed)

    async def _prune_loop(self) -> None:
        """Sweep the audit table, daily, for as long as the link exists.

        A failed sweep is logged and retried at the next tick: the rows are an
        operator's record, and a database that is busy or locked must cost the
        sweep rather than the link it rides on.
        """
        await asyncio.sleep(self._prune_first_delay_s)
        while True:
            try:
                async with self._sessions()() as session:
                    await prune_audit(session)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Cloud Link: could not prune the audit table: %s", e)
            await asyncio.sleep(self._prune_interval_s)

    # ---------------------------------------------------------------- the stop

    async def stop(self) -> None:
        """Take the link down and leave nothing of it behind.

        Idempotent: stopping something that is not running is not an error, so
        the lifespan and the settings routes can both call it without asking
        first.

        ⚠️ **The cancel is the mechanism, not a fallback.** See the module
        docstring: waiting for the stop event alone can take ~35 s in a
        handshake. The event is set first anyway, so a client already parked on
        it returns through its own code rather than through a cancellation.
        """
        async with self._guard():
            task, uplink, stop_event = self._task, self._uplink, self._stop_event
            hub = self._registered_with
            self._task = None
            self._client = None
            self._uplink = None
            self._stop_event = None
            self._registered_with = None

            if stop_event is not None:
                stop_event.set()
            if uplink is not None and hub is not None:
                # Stop feeding it immediately — before the await below, which
                # is a scheduling point the broadcast path can run inside.
                # Removing a listener that the task's own ``finally`` already
                # removed is deliberately harmless.
                hub.remove_internal_listener(uplink.feed)
            if task is None:
                return

            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            logger.info("Cloud Link: stopped")

    async def restart(self) -> None:
        """Stop, then start. What the settings page calls after a change.

        ⚠️ The reconnect backoff starts over: ``run`` resets it on entry. That
        is accepted — a restart is somebody deciding to restart, and a fresh
        decision deserves a fresh first attempt rather than the five-minute
        wait the previous one had escalated to.
        """
        await self.stop()
        await self.start()

    # -------------------------------------------------------------- the asking

    async def request_snapshot(self) -> None:
        """Re-read the publish set and have the portal re-told the whole farm.

        Called after the allowlist changes. Two halves, and they are asymmetric
        on purpose:

        ⚠️ **A removal applies here and now; an addition waits for the
        snapshot.** The in-memory set is intersected with the saved one, so a
        printer that was just unticked stops being published on the very next
        ``drain`` — the allowlist's whole job is keeping a machine off the
        internet, and "within a pump cycle" is not the same promise. Additions
        are left to ``build_snapshot``, which is the only place that also
        checks the printer is available (``is_active AND NOT archived``);
        adding here would publish an archived printer until the snapshot
        arrived to take it away again.

        Silent when no link is running: the settings route calls this whether
        or not the farm is connected, and a disabled link has nothing to tell.
        """
        uplink = self._uplink
        if uplink is None:
            return
        async with self._sessions()() as session:
            saved = await get_publish_set(session)
        uplink.set_publish_set(uplink.published & saved)
        if self._client is not None:
            self._client.request_snapshot()

    async def status(self, session: AsyncSession) -> dict:
        """What the settings page shows about the link.

        ⚠️ Takes a session — every field but one is a column, and reading them
        through a session the caller already holds keeps this out of the
        business of opening connections on a request path. ``connected`` is the
        exception: it is the client's advisory flag, in memory, and it is False
        whenever no task is running rather than whatever the last client
        happened to leave behind.
        """
        config = await get_config(session)
        return {
            "enabled": bool(config.enabled),
            "paired": bool(config.instance_id),
            "connected": bool(self._client.connected) if self.running and self._client else False,
            "revoked": bool(config.revoked),
            "last_error": config.last_error,
            "last_connected_at": config.last_connected_at,
        }


#: The one link this instance has. Started and stopped by the application
#: lifespan; restarted by the settings routes.
cloud_link_service = CloudLinkService()
