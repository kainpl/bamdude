"""Own the optional SDK manager, its requests and its key-connection observations."""

import asyncio
import json
import logging
import time
import uuid
from collections import OrderedDict, deque
from datetime import datetime, timezone

from fastapi import HTTPException
from starlette.responses import JSONResponse, Response

from backend.app.mcp_server.auth import authenticate
from backend.app.mcp_server.config import McpConfig, is_loopback_host, read_config
from backend.app.mcp_server.limits import (
    CALLS_PER_MINUTE_PER_KEY,
    CONCURRENT_CALLS_PER_KEY,
    CONCURRENT_CALLS_PER_PROCESS,
)
from backend.app.services.queue_sources import current_epoch

logger = logging.getLogger(__name__)


class McpRuntime:
    def __init__(self):
        self.config = McpConfig()
        self.state = "disabled"
        self.reason = None
        self._sdk_app = None
        self._manager_task = None
        self._stop = None
        self._lock = asyncio.Lock()
        self._requests: set[asyncio.Task] = set()
        self._auth_tasks: set[asyncio.Task] = set()
        self._key_calls: dict[int, int] = {}
        self._rate: OrderedDict[int, deque] = OrderedDict()
        self._connected: OrderedDict[int, datetime] = OrderedDict()
        self._admitted = 0
        self._domain_calls = 0
        self._held = False
        self._restore_hold = False

    async def reload(self, *, after_restore=False):
        async with self._lock:
            if self._restore_hold and not after_restore:
                return False
            if after_restore:
                self._restore_hold = False
            try:
                config = await read_config()
            except Exception:
                await self._configure_locked(McpConfig())
                self.state, self.reason = "unavailable", "configuration_unavailable"
                return False
            return await self._configure_locked(config)

    async def configure(self, config: McpConfig):
        async with self._lock:
            if self._restore_hold and config.enabled:
                return False
            return await self._configure_locked(config)

    async def _configure_locked(self, config: McpConfig):
        self._held = True
        if not await self._drain():
            self.state, self.reason = "stopping", "readers_still_running"
            return False
        if self._manager_task is not None:
            self._stop.set()
            done, _ = await asyncio.wait({self._manager_task}, timeout=10)
            if not done:
                self.state, self.reason = "stopping", "runtime_still_running"
                return False
            self._manager_task = None
            self._sdk_app = None
        self.config = config
        self.reason = None
        if not config.enabled:
            self.state = "disabled"
            self._held = False
            return True
        self.state = "starting"
        ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._manager_task = asyncio.create_task(self._own_manager(ready), name="mcp-runtime")
        await ready.wait()
        self._held = False
        return self.state == "ready"

    async def _own_manager(self, ready):
        # AnyIO's manager scope is entered and exited by this SAME task.
        try:
            from backend.app.mcp_server.registry import create_server

            server = create_server()
            sdk_app = server.streamable_http_app(stateless_http=True, json_response=True)
            async with server.session_manager.run():
                self._sdk_app = sdk_app
                self.state = "ready"
                ready.set()
                await self._stop.wait()
        except Exception:
            self.state, self.reason = "unavailable", "runtime_unavailable"
            logger.warning("MCP runtime unavailable")
        finally:
            self._sdk_app = None
            ready.set()

    async def _drain(self):
        for task in tuple(self._requests):
            task.cancel()
        pending = self._requests | self._auth_tasks
        if pending:
            _, remaining = await asyncio.wait(pending, timeout=10)
            return not remaining
        return True

    async def quiesce_for_restore(self):
        self._restore_hold = True
        self._held = True  # close admission before the first await
        self.state, self.reason = "stopping", "restore_in_progress"
        self._connected.clear()  # restored key IDs may identify different keys
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=10)
        except TimeoutError:
            self.state, self.reason = "stopping", "configuration_read_still_running"
            return False
        try:
            drained = await self._drain()
            self.state, self.reason = "unavailable", "restore_hold" if drained else "readers_still_running"
            return drained
        finally:
            self._lock.release()

    async def shutdown(self):
        if await self.configure(McpConfig()):
            self._restore_hold = False
        self._connected.clear()
        self._rate.clear()

    def observed_connection(self, key_id: int):
        value = self._connected.get(key_id)
        return value.isoformat() if value else None

    def _observed(self, key_id):
        self._connected[key_id] = datetime.now(timezone.utc)
        self._connected.move_to_end(key_id)
        while len(self._connected) > 4096:
            self._connected.popitem(last=False)

    async def dispatch(self, scope, receive, send):
        if scope["path"] == "/mcp/" or not self.config.enabled:
            return await self._error(scope, receive, send, 404, "not_found")
        if self._held or self._restore_hold or self.state != "ready":
            return await self._error(scope, receive, send, 503, "unavailable")
        headers = scope["headers"]
        hosts = [value.decode("latin-1").lower() for name, value in headers if name.lower() == b"host"]
        origins = [value.decode("latin-1") for name, value in headers if name.lower() == b"origin"]
        if len(hosts) != 1 or (hosts[0] not in self.config.allowed_hosts and not is_loopback_host(hosts[0])):
            return await self._error(scope, receive, send, 403, "host_denied")
        if len(origins) > 1 or (origins and origins[0] not in self.config.allowed_origins):
            return await self._error(scope, receive, send, 403, "origin_denied")
        if origins:
            scope["bamdude_cors_origin"] = origins[0]
        if scope["method"] == "OPTIONS":
            response = Response(
                status_code=204,
                headers={
                    "Cache-Control": "no-store",
                    "Access-Control-Allow-Methods": "GET, POST, DELETE",
                    "Access-Control-Allow-Headers": "Authorization, X-API-Key, Content-Type, MCP-Protocol-Version",
                },
            )
            if origins:
                response.headers["Access-Control-Allow-Origin"] = origins[0]
            return await response(scope, receive, send)
        if self._admitted >= 16:
            return await self._error(scope, receive, send, 503, "busy")
        self._admitted += 1
        task = asyncio.create_task(self._serve(scope, receive, send), name="mcp-request")
        self._requests.add(task)
        try:
            async with asyncio.timeout(15):
                await task
        except TimeoutError:
            await self._error(scope, receive, send, 503, "deadline_exceeded")
        finally:
            self._requests.discard(task)
            self._admitted -= 1

    @staticmethod
    async def _error(scope, receive, send, status, code):
        headers = {"Cache-Control": "no-store"}
        if origin := scope.get("bamdude_cors_origin"):
            headers.update({"Access-Control-Allow-Origin": origin, "Vary": "Origin"})
        await JSONResponse({"code": code, "message": code.replace("_", " ")}, status_code=status, headers=headers)(
            scope, receive, send
        )

    async def _serve(self, scope, receive, send):
        request_id = uuid.uuid4().hex
        scope["bamdude_request_id"] = request_id
        started = time.monotonic()
        access = None
        counted = False
        status = 503
        response_bytes = 0
        operation = "protocol"
        try:
            # Own the validation task through a client cancellation. Its inner
            # hash thread is shielded by the shared resolver, so restore must
            # continue waiting for this task rather than just its requester.
            auth_task = asyncio.create_task(authenticate(scope), name="mcp-auth")
            self._auth_tasks.add(auth_task)

            def auth_done(completed):
                self._auth_tasks.discard(completed)
                if not completed.cancelled():
                    completed.exception()

            auth_task.add_done_callback(auth_done)
            access = await asyncio.shield(auth_task)
            scope["bamdude_diagnostic_access"] = access
            self._observed(access.key_id)
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > 32768:
                    raise HTTPException(413, "request_too_large")
                if not message.get("more_body", False):
                    break
            domain = False
            try:
                payload = json.loads(body) if body else None
                domain = isinstance(payload, dict) and payload.get("method") == "tools/call"
                if domain:
                    params = payload.get("params")
                    name = params.get("name") if isinstance(params, dict) else None
                    operation = (
                        name if name in {"bamdude_get_capabilities", "bamdude_get_usage_guide"} else "unknown_tool"
                    )
            except (ValueError, UnicodeError):
                pass  # SDK owns malformed JSON/protocol responses
            if domain:
                now = time.monotonic()
                calls = self._rate.get(access.key_id)
                if calls is None:
                    # Evict only expired windows. Churning keys must not erase
                    # another key's live quota or allocate unbounded state.
                    while self._rate:
                        first = next(iter(self._rate.values()))
                        if first and first[-1] > now - 60:
                            break
                        self._rate.popitem(last=False)
                    if len(self._rate) >= 4096:
                        raise HTTPException(503, "busy")
                    calls = self._rate[access.key_id] = deque()
                self._rate.move_to_end(access.key_id)
                while calls and calls[0] <= now - 60:
                    calls.popleft()
                if len(calls) >= CALLS_PER_MINUTE_PER_KEY:
                    raise HTTPException(429, "rate_limited")
                if (
                    self._domain_calls >= CONCURRENT_CALLS_PER_PROCESS
                    or self._key_calls.get(access.key_id, 0) >= CONCURRENT_CALLS_PER_KEY
                ):
                    raise HTTPException(503, "busy")
                calls.append(now)
                self._domain_calls += 1
                self._key_calls[access.key_id] = self._key_calls.get(access.key_id, 0) + 1
                counted = True

            delivered = False

            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            messages = []

            async def collect(message):
                nonlocal response_bytes, status
                if message["type"] == "http.response.start":
                    status = message["status"]
                    message["headers"] = list(message.get("headers", [])) + [(b"cache-control", b"no-store")]
                    if origin := scope.get("bamdude_cors_origin"):
                        message["headers"] += [(b"access-control-allow-origin", origin.encode()), (b"vary", b"Origin")]
                if message["type"] == "http.response.body":
                    response_bytes += len(message.get("body", b""))
                    if response_bytes > 131072:
                        raise HTTPException(503, "result_too_large")
                messages.append(message)

            async with asyncio.timeout(10):
                await self._sdk_app(scope, replay, collect)
            if access.epoch != current_epoch() or self._held:
                raise HTTPException(503, "state_changed")
            for message in messages:
                await send(message)
        except HTTPException as exc:
            status = exc.status_code
            code = (
                exc.detail
                if isinstance(exc.detail, str)
                and exc.detail in {"request_too_large", "rate_limited", "busy", "result_too_large", "state_changed"}
                else ("unauthorized" if status == 401 else "forbidden" if status == 403 else "unavailable")
            )
            await self._error(scope, receive, send, status, code)
        except TimeoutError:
            await self._error(scope, receive, send, 503, "deadline_exceeded")
        except Exception:
            # Never send/log str(exc): SDK and file failures may contain secrets.
            await self._error(scope, receive, send, 503, "source_unavailable")
        finally:
            if counted:
                self._domain_calls -= 1
                self._key_calls[access.key_id] -= 1
                if self._key_calls[access.key_id] == 0:
                    del self._key_calls[access.key_id]
            logger.info(
                "MCP read request=%s key=%s tool=%s status=%s duration=%.3f bytes=%s",
                request_id,
                access.key_id if access else None,
                operation,
                status,
                time.monotonic() - started,
                response_bytes,
            )


mcp_runtime = McpRuntime()


class McpMiddleware:
    def __init__(self, app, runtime=None):
        self.app = app
        self.runtime = runtime or mcp_runtime

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] in {"/mcp", "/mcp/"}:
            return await self.runtime.dispatch(scope, receive, send)
        return await self.app(scope, receive, send)
