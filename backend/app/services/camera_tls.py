"""Owned TCP -> TLS camera proxy lifecycle.

Adapted from Bambuddy #2968/#3001, with bounded transport shutdown as well as
task cancellation. A finished coroutine alone does not release an SSL socket:
a peer that ignores close_notify otherwise holds it for Python's shutdown timer.
"""

import asyncio
import logging
import ssl
from weakref import WeakKeyDictionary

from backend.app.services.camera_cleanup import await_cleanup, own_task

logger = logging.getLogger(__name__)

_WRITER_CLOSE_GRACE = 1.0
_PROXY_CLOSE_TIMEOUT = 2.0
_proxy_states: WeakKeyDictionary = WeakKeyDictionary()


def rewrite_rtsp_request_url(data: bytes, proxy_url: bytes, real_url: bytes) -> bytes:
    """Rewrite the first RTSP request line only; preserve Digest URI and RTP."""
    rtsp_marker = b" RTSP/1.0"
    if rtsp_marker not in data:
        return data
    lines = data.split(b"\r\n")
    for i, line in enumerate(lines):
        if line.endswith(rtsp_marker):
            lines[i] = line.replace(proxy_url, real_url)
            break
    return b"\r\n".join(lines)


async def _wait_until(tasks, deadline):
    pending = {task for task in tasks if not task.done()}
    if pending:
        _, pending = await asyncio.wait(pending, timeout=max(0, deadline - asyncio.get_running_loop().time()))
    return pending


async def _writer_closed(writer):
    try:
        await writer.wait_closed()
    except OSError:
        # connection_lost(exc) also completes StreamReaderProtocol._closed.
        pass


class _ProxyState:
    def __init__(self, host, port):
        self.target = f"{host}:{port}"
        self.local_port = 0
        self.connections: set[_Connection] = set()
        self.closing = False
        self.closed = False
        self.failure: str | None = None
        self.deadline: float | None = None
        self.close_task: asyncio.Task | None = None
        self.warned = False

    def forced_abort(self, count, started):
        if not self.warned:
            self.warned = True
            logger.warning(
                "TLS proxy forced transport abort [target=%s proxy_port=%s elapsed=%.3fs transports=%s]",
                self.target,
                self.local_port,
                asyncio.get_running_loop().time() - started,
                count,
            )

    def failed(self):
        if self.failure is None:
            self.failure = "TLS proxy teardown deadline exceeded"
            logger.error(
                "%s [target=%s proxy_port=%s connections=%s transports=%s]",
                self.failure,
                self.target,
                self.local_port,
                len(self.connections),
                sum(sum(not task.done() for task in conn.writer_waits) for conn in self.connections),
            )


class _Connection:
    def __init__(self, state, reader, writer):
        self.state = state
        self.reader = reader
        self.writers = [writer]
        self.handler: asyncio.Task | None = None
        self.forwarders: list[asyncio.Task] = []
        self.writer_waits: list[asyncio.Task] = []
        self.cleanup: asyncio.Task | None = None
        state.connections.add(self)  # before the handler's first execution

    def start_cleanup(self):
        if self.cleanup is None:
            self.cleanup = own_task(self._finish(), name="camera-tls-connection-close")
            self.cleanup.add_done_callback(lambda _: self.release())
        return self.cleanup

    def release(self):
        # A handler cancelled before its first instruction never enters finally.
        self.start_cleanup()
        if (self.handler is None or self.handler.done()) and self.cleanup.done():
            if not self.cleanup.cancelled() and self.cleanup.exception() is None:
                self.state.connections.discard(self)

    async def _finish(self):
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = min(started + _PROXY_CLOSE_TIMEOUT, self.state.deadline or float("inf"))
        for task in self.forwarders:
            task.cancel()
        for writer in self.writers:
            try:
                writer.close()
            except (OSError, RuntimeError):
                try:
                    writer.transport.abort()
                except (OSError, RuntimeError):
                    pass  # still require wait_closed completion below
            self.writer_waits.append(own_task(_writer_closed(writer), name="camera-tls-writer-close"))
        pending = await _wait_until(self.writer_waits, min(started + _WRITER_CLOSE_GRACE, deadline))
        unfinished = pending | {
            task for task in self.writer_waits if task.done() and (task.cancelled() or task.exception() is not None)
        }
        if unfinished:
            self.state.forced_abort(len(unfinished), started)
            for writer, task in zip(self.writers, self.writer_waits, strict=True):
                if task in unfinished:
                    try:
                        writer.transport.abort()
                    except (OSError, RuntimeError):
                        pass
        pending = await _wait_until([*self.writer_waits, *self.forwarders], deadline)
        if pending or any(task.cancelled() or task.exception() is not None for task in self.writer_waits):
            # Do not cancel wait_closed: cancellation of the protocol's closure
            # Future is not evidence of connection_lost. Retain unfinished owners.
            self.state.failed()
            raise TimeoutError(self.state.failure)


async def _forward(reader, writer, proxy_url=None, real_url=None):
    try:
        while data := await reader.read(65536):
            if proxy_url is not None:
                data = rewrite_rtsp_request_url(data, proxy_url, real_url)
            writer.write(data)
            await writer.drain()
    except (OSError, RuntimeError):
        # uvloop raises RuntimeError for writes to a dead handle.
        pass


async def _handle(conn, target_host, target_port, ssl_ctx):
    try:
        if conn.state.closing:
            return
        tls_reader, tls_writer = await asyncio.wait_for(
            asyncio.open_connection(target_host, target_port, ssl=ssl_ctx),
            timeout=10.0,
        )
        conn.writers.append(tls_writer)
        proxy_url = f"rtsp://127.0.0.1:{conn.state.local_port}".encode()
        real_url = f"rtsps://{target_host}:{target_port}".encode()
        conn.forwarders = [
            own_task(_forward(conn.reader, tls_writer, proxy_url, real_url), name="camera-tls-to-server"),
            own_task(_forward(tls_reader, conn.writers[0]), name="camera-tls-to-client"),
        ]
        await asyncio.wait(conn.forwarders, return_when=asyncio.FIRST_COMPLETED)
    except (OSError, TimeoutError, RuntimeError) as exc:
        logger.debug(
            "TLS proxy connection failed [target=%s proxy_port=%s exception=%s]",
            conn.state.target,
            conn.state.local_port,
            type(exc).__name__,
        )
    finally:
        try:
            await await_cleanup(conn.start_cleanup())
        except TimeoutError:
            pass  # state retains the failure; close_tls_proxy reports it


async def create_tls_proxy(target_host: str, target_port: int) -> tuple[int, asyncio.Server]:
    """Expose printer RTSPS through OpenSSL, avoiding ffmpeg's GnuTLS issues.

    Caller owns the returned server and MUST await close_tls_proxy(server).
    SSL verification and RTSP request rewriting match the original proxy.
    """
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    state = _ProxyState(target_host, target_port)

    def accept(reader, writer):
        conn = _Connection(state, reader, writer)
        if state.closing:
            writer.transport.abort()
            conn.start_cleanup()
            return
        conn.handler = own_task(_handle(conn, target_host, target_port, ssl_ctx), name="camera-tls-handler")
        conn.handler.add_done_callback(lambda _: conn.release())

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    state.local_port = server.sockets[0].getsockname()[1]
    # uvloop Server has slots. Never attach application attributes to it, nor
    # retain a strong server reference in this weak dictionary's value.
    _proxy_states[server] = state
    logger.debug("TLS proxy listening [target=%s proxy_port=%s]", state.target, state.local_port)
    return state.local_port, server


async def _finish_proxy(server, state):
    listener = own_task(server.wait_closed(), name="camera-tls-listener-close")
    # Accept callbacks already queued when server.close() ran must register first.
    await asyncio.sleep(0)
    while state.connections:
        connections = list(state.connections)
        tasks = []
        for conn in connections:
            if conn.handler is not None:
                if not conn.handler.done():
                    conn.handler.cancel()
                tasks.append(conn.handler)
            tasks.append(conn.start_cleanup())
        if await _wait_until(tasks, state.deadline):
            break
        for conn in connections:
            conn.release()
        if state.failure or any(conn.cleanup.cancelled() or conn.cleanup.exception() for conn in connections):
            break
        await asyncio.sleep(0)
    pending = await _wait_until([listener], state.deadline)
    if state.connections or pending or state.failure or listener.cancelled() or listener.exception() is not None:
        state.failed()
        raise TimeoutError(state.failure)
    state.closed = True
    logger.debug("TLS proxy closed [target=%s proxy_port=%s]", state.target, state.local_port)


async def close_tls_proxy(server: asyncio.Server) -> None:
    """Close listener, handlers AND transports within one shared deadline.

    Cancellation is re-raised after the owned coordinator finishes. Concurrent
    callers share it; a timeout is sticky and never reported as successful close.
    """
    state = _proxy_states[server]
    if state.closed:
        return
    if state.close_task is None:
        if state.failure and state.closing:
            raise TimeoutError(state.failure)
        state.closing = True
        state.deadline = asyncio.get_running_loop().time() + _PROXY_CLOSE_TIMEOUT
        server.close()
        state.close_task = own_task(_finish_proxy(server, state), name="camera-tls-proxy-close")
        # Drop the finished task (and any exception traceback containing server).
        state.close_task.add_done_callback(lambda _: setattr(state, "close_task", None))
    await await_cleanup(state.close_task)
