"""Cloud Link client loop — one agent, one portal, one socket at a time.

This is the module that owns the connection: it derives the WebSocket URL from
the configured portal, says hello, and then runs three concurrent jobs over the
same socket — a heartbeat, a pump that empties the uplink, and a reader that
turns inbound ``cmd`` frames into dispatched work. When the socket ends, it
decides whether to come back.

Four rulings shape everything below.

**The URL is parsed, never pattern-matched.** ``portal_url`` is whatever a user
typed into a settings field: it may carry an uppercase scheme, a port, and a
path prefix if the portal sits behind a proxy. :func:`ws_url` splits it, swaps
only the scheme, and appends :data:`LINK_PATH` below the existing path. A
``startswith("https")`` / ``replace`` pair gets an uppercase scheme wrong and a
proxied path wrong, and both failures surface to the user as "the portal is
down".

**Only ``revoked`` stops the loop.** A ``hello_err`` is the portal declining the
handshake, and three of the codes mean different things. ``revoked`` is a
*decision*: the credential has been thrown away and no amount of retrying will
bring it back, so the agent persists the fact and stops until somebody changes
the settings. ``bad_credentials`` and ``unsupported_version`` are *outcomes* —
a portal mid-deploy, reading a replica, or briefly rolled back can produce
either for a credential that is perfectly good — so they take the full backoff
and try again. Stopping on them would need a human to notice; reconnecting
costs one socket every few minutes and heals itself.

**A reconnect is a fresh start, not a resumption.** Every successful hello
clears the uplink's transient state and sends a new snapshot, because
everything the portal knew about this farm was told to it over a socket that no
longer exists. The order matters: reset, then build, then send — a frame left
over from the dead socket would otherwise arrive *after* the picture that
replaced it and overwrite a current reading with a stale one.

**The reader outlives what it dispatches.** A handler that raises costs one
command, never the link, and the frame it failed on is left unanswered on
purpose: the handler got far enough to raise, so the agent does not know
whether the work happened and ``ok=false`` would be a claim it cannot make. The
portal times that request out, which is the honest outcome, and the
``cmd:failed`` audit row is what an operator finds afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from aiohttp import WSMsgType
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.config import APP_VERSION
from backend.app.services.cloud_link.commands import (
    ALLOWED_COMMANDS,
    MAX_AUDITED_NAME,
    CommandContext,
    dispatch,
)
from backend.app.services.cloud_link.schemas import (
    AnyFrame,
    Cmd,
    CmdResult,
    CmdResultData,
    Heartbeat,
    Hello,
    HelloData,
    HelloErr,
    HelloOk,
    frame_timestamp,
    make_frame,
    new_frame_id,
    parse_frame,
)
from backend.app.services.cloud_link.store import get_config, get_secret, write_audit
from backend.app.services.cloud_link.uplink import Uplink

logger = logging.getLogger(__name__)

#: Appended below whatever path the configured portal URL already carries.
LINK_PATH = "/link/v1"

#: How a portal URL's scheme becomes a socket's. Anything not listed is passed
#: through untouched — an unknown scheme is a URL the user must fix, and
#: guessing at it would open a connection to something they did not ask for.
WS_SCHEMES = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}

#: The envelope version this agent speaks. A list on the wire, so a later agent
#: can offer two while the portal picks.
ENVELOPE_VERSION = 1

#: Bounds establishing the TCP/TLS connection, and nothing else — the socket
#: itself is long-lived, so a total timeout would kill a healthy link.
CONNECT_TIMEOUT_S = 15.0

#: How long the portal has to answer ``hello``. Silence is not a refusal, so it
#: ends in a reconnect like any other failed attempt.
HANDSHAKE_TIMEOUT_S = 20.0

#: Used when ``hello_ok`` carries a nonsensical interval. A portal answering
#: ``0`` would otherwise turn the heartbeat task into a busy loop.
DEFAULT_HEARTBEAT_INTERVAL_S = 30.0

#: How long the pump waits after emptying the uplink before looking again. The
#: uplink has no "something arrived" signal — it is fed from a synchronous
#: broadcast callback — so this is a poll, and 0.2 s is imperceptible next to
#: the per-printer status throttle the portal sets.
IDLE_SLEEP_S = 0.2

#: Reconnection backoff: 1 s, doubling, capped at five minutes, ±20 % jitter so
#: a farm of agents does not return as one synchronised wave after a portal
#: restart.
BACKOFF_BASE_S = 1.0
BACKOFF_FACTOR = 2.0
BACKOFF_CAP_S = 300.0
BACKOFF_JITTER = 0.2

#: How many unknown commands may reach :func:`dispatch` — and therefore write an
#: audit row — on ONE connection. Past it the reader answers them itself. See
#: :meth:`CloudLinkClient._reader_loop`.
UNKNOWN_COMMAND_AUDIT_LIMIT = 5

#: What the reader hands back to the connection when it ends.
_CLOSED = "closed"
_REVOKED = "revoked"
_ReaderOutcome = Literal["closed", "revoked"]


def ws_url(portal_url: str) -> str:
    """The link's WebSocket URL, derived from the configured portal URL.

    Only the scheme is rewritten; host, port and path prefix are the user's and
    are carried through. See the module docstring for why this is a parse.
    """
    parts = urlsplit(portal_url.strip())
    scheme = WS_SCHEMES.get(parts.scheme.lower(), parts.scheme.lower())
    path = parts.path.rstrip("/") + LINK_PATH
    # Query and fragment belong to a URL somebody pasted, not to this socket.
    return urlunsplit((scheme, parts.netloc, path, "", ""))


def refuse_unknown(cmd_frame: Cmd) -> CmdResult:
    """The answer to an unknown command, built without the dispatcher.

    ⚠️ **This must stay identical to what :func:`dispatch` returns for a name
    outside the allowlist** — it is what the portal gets once the per-connection
    audit cap is reached, and a divergence would be invisible until somebody
    compared their sixth refusal with their seventh. A test pins the two equal.
    """
    return CmdResult(
        v=1,
        id=new_frame_id(),
        ts=frame_timestamp(),
        type="cmd_result",
        re=cmd_frame.id,
        data=CmdResultData(ok=False, error="unknown_command"),
    )


def _utcnow() -> datetime:
    """Naive UTC, because ``cloud_link.last_connected_at`` is naive.

    Same reasoning as ``store._utcnow`` — an aware value works on the
    application engine and silently mis-sorts on one built without the
    offset-stripping listener.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CloudLinkClient:
    """The connection to one portal, from hello to reconnect.

    One instance per link, driven by exactly one :meth:`run`. Everything with a
    duration — the heartbeat, the backoff, the pump's idle wait, the two
    timeouts — is injectable, so the tests exercise the real loop at
    millisecond scale instead of mocking it away.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        uplink: Uplink,
        heartbeat_override_s: float | None = None,
        backoff_base_s: float = BACKOFF_BASE_S,
        backoff_cap_s: float = BACKOFF_CAP_S,
        idle_sleep_s: float = IDLE_SLEEP_S,
        connect_timeout_s: float = CONNECT_TIMEOUT_S,
        handshake_timeout_s: float = HANDSHAKE_TIMEOUT_S,
        unknown_command_limit: int = UNKNOWN_COMMAND_AUDIT_LIMIT,
        rng: Callable[[], float] = random.random,
    ):
        """
        Args:
            session_factory: Opens a database session. A **factory**: this loop
                holds no session across an await it does not control, and the
                audit writes have to survive a failed statement elsewhere.
            uplink: The uplink for this link. Registering it as a broadcast
                listener is the service's job, not this one's — the client only
                drains it.
            heartbeat_override_s: Ignore the portal's interval and use this.
                For tests; production takes what ``hello_ok`` says.
            rng: Source of the backoff jitter, injected so the sequence is
                pinnable.
        """
        self._session_factory = session_factory
        self._uplink = uplink
        self._ctx = CommandContext(session_factory=session_factory, uplink=uplink)
        self._heartbeat_override_s = heartbeat_override_s
        self._heartbeat_interval_s = DEFAULT_HEARTBEAT_INTERVAL_S
        self._backoff_base_s = backoff_base_s
        self._backoff_cap_s = backoff_cap_s
        self._idle_sleep_s = idle_sleep_s
        self._connect_timeout_s = connect_timeout_s
        self._handshake_timeout_s = handshake_timeout_s
        self._unknown_command_limit = unknown_command_limit
        self._rng = rng
        self._attempt = 0
        # Serialises the three tasks that share one socket. aiohttp writes a
        # frame in one call today, but that is an implementation detail and
        # compression changes it — an interleaved write is a corrupt frame the
        # portal cannot parse and nothing on this side would notice.
        self._send_lock = asyncio.Lock()
        #: Whether the handshake currently holds. Read by the service for its
        #: status endpoint; never a decision input here.
        self.connected = False

    # ---------------------------------------------------------------- the run

    async def run(self, stop_event: asyncio.Event) -> None:
        """Keep this farm connected until ``stop_event`` — or until revoked.

        Returns rather than raises on every failure it knows about: the caller
        is a long-lived service task, and a loop that ends on the first refused
        handshake would leave a farm offline until the next restart.
        """
        self._attempt = 0
        while not stop_event.is_set():
            try:
                reconnect = await self._connect_once(stop_event)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Cloud Link: connection attempt failed — %s: %s", type(e).__name__, e)
                await self._record_error(f"{type(e).__name__}: {e}")
                await self._audit("disconnect", f"connection attempt failed: {e}", ok=False)
                reconnect = True
            finally:
                self.connected = False

            if not reconnect or stop_event.is_set():
                break
            await self._backoff_sleep(stop_event)

        logger.info("Cloud Link: client loop stopped")

    async def _connect_once(self, stop_event: asyncio.Event) -> bool:
        """One whole connection. Returns whether to try again after it.

        The credential is read fresh on every attempt rather than captured at
        start: a re-pair while the loop is backing off must be picked up by the
        next attempt, not by a restart.
        """
        async with self._session_factory() as session:
            config = await get_config(session)
            portal_url = config.portal_url
            instance_id = config.instance_id
            secret = await get_secret(session)

        if not instance_id or not secret:
            # Not paired: there is nothing to say hello *with*. Retrying would
            # be one connection per backoff window for as long as the farm
            # stays unpaired, and pairing restarts this loop anyway.
            logger.info("Cloud Link: no credential stored — the client loop has nothing to connect with")
            await self._audit("disconnect", "not paired — nothing to connect with", ok=False)
            return False

        url = ws_url(portal_url)
        timeout = aiohttp.ClientTimeout(total=None, connect=self._connect_timeout_s)
        logger.info("Cloud Link: connecting to %s", url)
        async with aiohttp.ClientSession(timeout=timeout) as http, http.ws_connect(url) as ws:
            return await self._converse(ws, url, instance_id, secret, stop_event)

    async def _converse(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        url: str,
        instance_id: str,
        secret: str,
        stop_event: asyncio.Event,
    ) -> bool:
        """Handshake, then the live phase. Returns whether to reconnect."""
        await self._send(ws, self._hello(instance_id, secret))
        greeting = await self._await_greeting(ws)

        if isinstance(greeting, HelloErr):
            code = greeting.data.code
            if code == "revoked":
                await self._teardown_revoked("the portal revoked this instance")
                return False
            # See the module docstring: an outcome, not a decision — retry.
            reason = f"the portal refused the handshake: {code}"
            logger.warning("Cloud Link: %s", reason)
            await self._record_error(reason)
            await self._audit("hello_err", reason, direction="down", ok=False)
            return True

        if not isinstance(greeting, HelloOk):
            reason = "the portal did not answer the handshake"
            logger.warning("Cloud Link: %s", reason)
            await self._record_error(reason)
            await self._audit("disconnect", reason, ok=False)
            return True

        await self._after_hello(ws, greeting, url)
        return await self._live(ws, stop_event)

    def _hello(self, instance_id: str, secret: str) -> Hello:
        return Hello(
            v=1,
            id=new_frame_id(),
            ts=frame_timestamp(),
            type="hello",
            data=HelloData(
                instance_id=instance_id,
                secret=secret,
                agent_version=APP_VERSION,
                envelope_versions=[ENVELOPE_VERSION],
                # Phase 0 claims nothing beyond the envelope itself. The field
                # exists so a later agent can offer a feature without the
                # portal having to infer it from a version number.
                capabilities=[],
            ),
        )

    async def _await_greeting(self, ws: aiohttp.ClientWebSocketResponse) -> HelloOk | HelloErr | None:
        """The portal's answer to ``hello``, or ``None`` if there wasn't one.

        A timeout, a closed socket, a malformed frame and a well-formed frame
        of the wrong type all collapse to ``None`` — they differ only in the
        log line, and every one of them means the handshake did not complete.
        """
        try:
            msg = await asyncio.wait_for(ws.receive(), self._handshake_timeout_s)
        except TimeoutError:
            logger.warning("Cloud Link: the portal did not answer hello within %.1fs", self._handshake_timeout_s)
            return None

        if msg.type is not WSMsgType.TEXT:
            logger.warning("Cloud Link: the portal answered hello with %s, not a frame", msg.type)
            return None

        frame = self._parse(msg.data)
        if isinstance(frame, (HelloOk, HelloErr)):
            return frame
        logger.warning("Cloud Link: the portal answered hello with %s", getattr(frame, "type", "nothing parseable"))
        return None

    async def _after_hello(self, ws: aiohttp.ClientWebSocketResponse, hello_ok: HelloOk, url: str) -> None:
        """Everything a successful handshake settles, in the order it matters.

        The reset comes before the snapshot is *built*, not merely before it is
        sent — building it also reseeds the uplink's identity and connection
        caches, and a stale outbox frame drained between the two would be
        describing the world the snapshot is in the middle of replacing.
        """
        interval = self._heartbeat_override_s
        if interval is None:
            interval = hello_ok.data.heartbeat_interval_s
            if interval <= 0:
                logger.warning(
                    "Cloud Link: the portal asked for a %.3fs heartbeat — using %.1fs instead",
                    interval,
                    DEFAULT_HEARTBEAT_INTERVAL_S,
                )
                interval = DEFAULT_HEARTBEAT_INTERVAL_S
        self._heartbeat_interval_s = interval

        throttle = hello_ok.data.throttle_min_interval_s
        if throttle >= 0:
            self._uplink.min_interval_s = throttle

        self._attempt = 0  # the backoff is about failures, and this was not one
        self.connected = True
        self._uplink.reset_transient()
        await self._record_connected()
        await self._audit("connect", f"connected to {url}")
        await self._send_snapshot(ws)

    async def _live(self, ws: aiohttp.ClientWebSocketResponse, stop_event: asyncio.Event) -> bool:
        """The three concurrent jobs, until the first of them ends.

        Whatever ends first, ALL of them are cancelled and awaited in the
        ``finally`` — the socket is about to close under them, and a task left
        running would either write to a dead connection or outlive
        :meth:`run` entirely.
        """
        stop_waiter = asyncio.create_task(stop_event.wait())
        reader = asyncio.create_task(self._reader_loop(ws))
        heartbeat = asyncio.create_task(self._heartbeat_loop(ws))
        pump = asyncio.create_task(self._pump_loop(ws))
        tasks = [reader, heartbeat, pump, stop_waiter]

        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

            if stop_waiter.done():
                logger.info("Cloud Link: stopping — closing the link")
                await self._audit("disconnect", "the agent is shutting down")
                return False

            reason = "the link dropped"
            if reader.done() and not reader.cancelled():
                failure = reader.exception()
                if failure is None:
                    if reader.result() == _REVOKED:
                        await self._teardown_revoked("the portal revoked this instance")
                        return False
                    reason = "the portal closed the link"
                else:
                    reason = f"the reader failed — {type(failure).__name__}: {failure}"
            else:
                failure = next(
                    (t.exception() for t in (heartbeat, pump) if t.done() and not t.cancelled() and t.exception()),
                    None,
                )
                if failure is not None:
                    reason = f"the link failed while sending — {type(failure).__name__}: {failure}"

            logger.warning("Cloud Link: %s", reason)
            await self._record_error(reason)
            await self._audit("disconnect", reason, ok=False)
            return True
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------- the reader

    async def _reader_loop(self, ws: aiohttp.ClientWebSocketResponse) -> _ReaderOutcome:
        """Inbound frames, until the socket ends or a revoke tears it down.

        ⚠️ **The unknown-command cap is a write bound, not a rate limit.**
        :func:`dispatch` audits every command it refuses, which is right for
        the handful an operator will ever see and wrong for a portal that has
        been taken over and is spraying names: each one would be a row in a
        table kept for a month. After :data:`UNKNOWN_COMMAND_AUDIT_LIMIT`
        refusals on one connection the reader answers them itself — the wire
        contract is unchanged, every request still gets its ``cmd_result``, and
        the table stops growing. The counter is per connection because a
        reconnect is the natural place for an operator's mistyped command to be
        forgiven, and an attacker gains only one further row per socket.
        """
        unknown = 0
        try:
            async for msg in ws:
                if msg.type is WSMsgType.ERROR:
                    logger.warning("Cloud Link: socket error: %s", ws.exception())
                    break
                if msg.type is not WSMsgType.TEXT:
                    continue

                frame = self._parse(msg.data)
                if frame is None:
                    continue
                if isinstance(frame, Heartbeat):
                    # The portal may beat back. Nothing to do, and certainly
                    # nothing to drop the link over.
                    continue
                if not isinstance(frame, Cmd):
                    logger.debug("Cloud Link: ignoring an inbound %s frame", frame.type)
                    continue

                if frame.data.cmd not in ALLOWED_COMMANDS:
                    unknown += 1
                    if unknown > self._unknown_command_limit:
                        await self._send(ws, refuse_unknown(frame))
                        continue

                outcome = await self._handle_cmd(ws, frame)
                if outcome is not None:
                    return outcome
        finally:
            if unknown:
                logger.info(
                    "Cloud Link: %d unknown command(s) refused on this connection (%d audited)",
                    unknown,
                    min(unknown, self._unknown_command_limit),
                )
        return _CLOSED

    async def _handle_cmd(self, ws: aiohttp.ClientWebSocketResponse, frame: Cmd) -> _ReaderOutcome | None:
        """Dispatch one command, answer it, then run whatever it implied.

        The result goes out **before** the post-action, always: ``resync``
        would otherwise build a snapshot on the reader task while the portal
        waited for its acknowledgement, and ``revoke`` would close the socket
        the acknowledgement had to leave through.
        """
        try:
            result, post_action = await dispatch(frame, self._ctx)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Deliberately unanswered — see the module docstring.
            logger.exception("Cloud Link: the '%s' command handler failed", frame.data.cmd)
            await self._audit(
                "cmd:failed",
                f"the {frame.data.cmd[:MAX_AUDITED_NAME]!r} command handler raised",
                direction="down",
                ok=False,
            )
            return None

        await self._send(ws, result)

        if post_action == "send_snapshot":
            await self._send_snapshot(ws)
        elif post_action == "teardown_revoked":
            return _REVOKED
        return None

    def _parse(self, raw: str) -> AnyFrame | None:
        """One inbound text message → a frame, or ``None`` with a log line.

        Anything unparseable costs one frame and nothing else. The link is this
        farm's only channel to its portal, so dropping it over a malformed
        message would hand whatever could inject one a way to keep the farm
        offline for as long as it kept injecting.
        """
        try:
            payload = json.loads(raw)
        except ValueError as e:
            logger.debug("Cloud Link: ignoring a message that is not JSON: %s", e)
            return None
        if not isinstance(payload, dict):
            logger.debug("Cloud Link: ignoring a %s where a frame was expected", type(payload).__name__)
            return None
        try:
            return parse_frame(payload)
        except ValueError as e:
            logger.debug("Cloud Link: ignoring an invalid frame: %s", e)
            return None

    # ------------------------------------------------------------- the senders

    async def _heartbeat_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Say we are still here, on the portal's interval.

        Sleeps first: the hello and the snapshot that just went out are better
        proof of liveness than a heartbeat sent on top of them.
        """
        while True:
            await asyncio.sleep(self._heartbeat_interval_s)
            await self._send(ws, Heartbeat(v=1, id=new_frame_id(), ts=frame_timestamp(), type="heartbeat"))

    async def _pump_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Empty the uplink, then wait, then empty it again.

        ⚠️ **Drain until it says ``None``.** One ``drain`` is not one cycle's
        work: a connection edge produces an event *and* the status behind it,
        and the uplink hands those over in two calls. Draining once per cycle
        would deliver the second one a full idle-sleep late, every time.
        """
        while True:
            frame = await self._uplink.drain()
            while frame is not None:
                await self._send(ws, frame)
                frame = await self._uplink.drain()
            await asyncio.sleep(self._idle_sleep_s)

    async def _send_snapshot(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Build the farm's full picture and send it. Its own session, held
        only for the read — the socket write is not the database's business."""
        async with self._session_factory() as session:
            snapshot = await self._uplink.build_snapshot(session)
        await self._send(ws, snapshot)

    async def _send(self, ws: aiohttp.ClientWebSocketResponse, frame: AnyFrame) -> None:
        async with self._send_lock:
            await ws.send_str(json.dumps(make_frame(frame)))

    # -------------------------------------------------------------- the record

    async def _record_connected(self) -> None:
        """Stamp the link as working and clear whatever last broke it."""
        try:
            async with self._session_factory() as session:
                config = await get_config(session)
                config.last_connected_at = _utcnow()
                config.last_error = None
                await session.commit()
        except Exception as e:
            logger.warning("Cloud Link: could not record the connection: %s", e)

    async def _record_error(self, reason: str) -> None:
        """What the settings page shows when the link is not up."""
        try:
            async with self._session_factory() as session:
                config = await get_config(session)
                config.last_error = reason
                await session.commit()
        except Exception as e:
            logger.warning("Cloud Link: could not record '%s': %s", reason, e)

    async def _teardown_revoked(self, reason: str) -> None:
        """Persist the revocation. The caller stops; nothing reconnects.

        Both ways in — a ``hello_err {code:"revoked"}`` and a ``revoke``
        command — end here, so the two halves of one event cannot drift apart.
        The socket is closed by the ``async with`` the caller is inside; this
        function only writes, and swallows a failure to do so, because a
        database that cannot be written to must not turn a revocation into an
        exception that the outer loop would read as "retry".
        """
        logger.warning("Cloud Link: %s — the link is down until this instance is paired again", reason)
        try:
            async with self._session_factory() as session:
                config = await get_config(session)
                config.revoked = True
                config.last_error = reason
                await session.commit()
        except Exception as e:
            logger.error("Cloud Link: could not persist the revocation: %s", e)
        await self._audit("revoked", reason, direction="down", ok=False)

    async def _audit(self, kind: str, summary: str, direction: str = "up", ok: bool = True) -> None:
        """One audit row, in its own session, failing silently.

        ``direction`` is "up" for things this agent did to the socket and
        "down" for things the portal did to us. A row is the operator's record
        and never a step of the protocol, so a busy or locked database must
        cost the record and not the connection.
        """
        try:
            async with self._session_factory() as session:
                await write_audit(session, direction=direction, kind=kind, summary=summary, ok=ok)
        except Exception as e:
            logger.warning("Cloud Link: could not write the '%s' audit row: %s", kind, e)

    # ------------------------------------------------------------- the backoff

    def _next_delay(self) -> float:
        """The next reconnect delay, and advance the sequence.

        Doubling from :data:`BACKOFF_BASE_S`, capped at :data:`BACKOFF_CAP_S`,
        then jittered by ±:data:`BACKOFF_JITTER`. The cap is applied *before*
        the jitter so the sequence is the documented one and the spread is a
        property of each delay rather than of the cap.
        """
        raw = min(self._backoff_base_s * (BACKOFF_FACTOR**self._attempt), self._backoff_cap_s)
        self._attempt += 1
        spread = 1.0 + BACKOFF_JITTER * (2.0 * self._rng() - 1.0)
        return max(0.0, raw * spread)

    async def _backoff_sleep(self, stop_event: asyncio.Event) -> None:
        """Wait out the backoff, but never past a stop.

        Waiting on the event rather than sleeping means a shutdown during a
        five-minute backoff returns immediately instead of five minutes later.
        """
        delay = self._next_delay()
        logger.info("Cloud Link: reconnecting in %.1fs", delay)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
