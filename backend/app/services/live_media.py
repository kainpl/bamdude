"""Owned private media bodies and the final, server-facing delivery boundary.

The worker/broadcaster remains the physical source owner. This module owns only
one viewer, its file/subscription lease and the actual ASGI body task.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field, replace
from functools import wraps
from typing import Any

import anyio
from fastapi import Depends, HTTPException, Request
from fastapi.dependencies.utils import get_parameterless_sub_dependant
from fastapi.routing import APIRoute, request_response
from starlette.datastructures import MutableHeaders
from starlette.responses import FileResponse, Response

from backend.app.services.live_authority import live_authority

logger = logging.getLogger(__name__)

# These are route templates, not an authorization allowlist. A successful body
# on one of them must have a handle even if an adapter is accidentally removed.
MEDIA_ROUTES = frozenset(
    {
        "/printers/{printer_id}/camera/stream",
        "/printers/{printer_id}/camera/snapshot",
        "/printers/{printer_id}/camera/plate-detection/references/{index}/thumbnail",
        "/cameras/{camera_id}/stream",
        "/cameras/{camera_id}/snapshot",
        "/archives/{archive_id}/thumbnail",
        "/archives/{archive_id}/timelapse",
        "/archives/{archive_id}/qrcode",
        "/archives/{archive_id}/plate-preview",
        "/archives/{archive_id}/plate-thumbnail/{plate_index}",
        "/archives/{archive_id}/project-image/{image_path:path}",
        "/library/files/{file_id}/card-file/{zip_path:path}",
        "/library/files/{file_id}/plate-thumbnail/{plate_index}",
        "/library/files/{file_id}/thumbnail",
        "/makerworld/thumbnail",
        "/makerworld/imports/{library_file_id}/cover",
        "/makerworld/imports/{library_file_id}/cover-variant",
        "/product-parts/{part_id}/image",
        "/products/{product_id}/files/{library_file_id}/plates/{plate_index}/objects/{identify_id}/image",
        "/printers/{printer_id}/camera-cover",
        "/products/{product_id}/attachment-image/{filename}",
        "/products/{product_id}/cover-image",
        "/projects/{project_id}/cover-image",
    }
)


def _template(path: str, prefix: str) -> str:
    return path[len(prefix) :] if prefix and path.startswith(prefix + "/") else path


def _pattern(template: str) -> re.Pattern:
    parts = re.split(r"(\{[^}]+\})", template)
    return re.compile(
        "^"
        + "".join(
            ".+" if part.endswith(":path}") else "[^/]+" if part.startswith("{") else re.escape(part) for part in parts
        )
        + "$"
    )


class _DeliveryStopped(Exception):
    """Internal control flow, consumed only by the outer delivery boundary."""


@dataclass(eq=False)
class MediaViewer:
    handle: Any
    body: asyncio.Task | None = None
    cleanup: asyncio.Task | None = None
    iterator: Any = None
    releases: list = field(default_factory=list)
    delivery_done: bool = False
    cleanup_done: bool = False
    cancel_sent: bool = False
    outer_task: asyncio.Task | None = None
    outer_started: bool = False
    outer_cancel_sent: bool = False
    outer_cancel_token: object = field(default_factory=object, repr=False)
    headers_started: bool = False
    disconnect_seen: bool = False

    def abort_outer(self):
        task = self.outer_task
        if (
            task is not None
            and task is not asyncio.current_task()
            and not task.done()
            and not self.outer_cancel_sent
            and (self.outer_started or self.disconnect_seen)
        ):
            self.outer_cancel_sent = True
            task.cancel(self.outer_cancel_token)

    def request_cleanup(self, _code=None):
        if self.cleanup is None:
            self.cleanup = live_authority.own(
                asyncio.create_task(self._cleanup(), name="live-media-cleanup"), cleanup=True
            )
        return self.cleanup

    def cancel_body(self):
        if (
            self.body is not None
            and self.body is not asyncio.current_task()
            and not self.body.done()
            and not self.cancel_sent
        ):
            self.cancel_sent = True
            self.body.cancel()

    def retired(self, _code=None):
        self.cancel_body()
        self.abort_outer()
        self.request_cleanup()

    def disconnected(self):
        self.disconnect_seen = True
        self.retired()

    def finish_delivery(self):
        self.delivery_done = True
        self._release_if_complete()

    def _release_if_complete(self):
        if self.delivery_done and self.cleanup_done:
            live_authority.release(self.handle)

    async def _cleanup(self):
        # This task never belongs to the response's AnyIO cancel scope. A body
        # finally only requests it: waiting here for the body cannot self-join.
        if self.body is not None:
            await asyncio.gather(self.body, return_exceptions=True)
        failure = None
        if self.iterator is not None:
            try:
                await self.iterator.aclose()
            except BaseException as exc:
                failure = exc
        for release in reversed(self.releases):
            try:
                result = release()
                if hasattr(result, "__await__"):
                    await result
            except BaseException as exc:
                failure = failure or exc
        if failure is not None:
            # Keep the handle charged. Restore must refuse an unproven drain.
            logger.warning("Live media cleanup incomplete: category=resource_cleanup")
            raise failure
        self.cleanup_done = True
        self._release_if_complete()


async def _join_cleanup(viewer: MediaViewer):
    task = viewer.request_cleanup()
    # Shield both AnyIO level cancellation and asyncio Task.cancel. Repeated
    # cancellation may stop this waiter, never the owned cleanup task itself.
    with anyio.CancelScope(shield=True):
        await asyncio.shield(task)


async def _descriptor(request):
    from backend.app.services.live_authority import descriptor_for_request

    return await descriptor_for_request(request)


class _MjpegBody(Response):
    def __init__(self, factory):
        super().__init__(
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
        self.factory = factory
        # This body is unbounded; Response(content=b"") would otherwise add 0.
        self.raw_headers = [(key, value) for key, value in self.raw_headers if key != b"content-length"]

    async def run(self, viewer, scope, receive, send):
        iterator = await self.factory(viewer)
        viewer.iterator = iterator
        await send({"type": "http.response.start", "status": 200, "headers": self.raw_headers})
        async for chunk in iterator:
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
        if self.background is not None:
            await self.background()


class _OwnedFileBody(FileResponse):
    """Keep Starlette Range/header semantics; own actual file I/O and close."""

    def __init__(self, response):
        self.__dict__.update(response.__dict__)
        self.file = None
        self.pending: set[asyncio.Task] = set()

    async def _io(self, function, *args):
        task = live_authority.own(asyncio.create_task(asyncio.to_thread(function, *args), name="live-media-file-io"))
        self.pending.add(task)
        try:
            return await asyncio.shield(task)
        finally:
            # Keep cancelled waiters' real operations until cleanup joins them.
            if task.done():
                self.pending.discard(task)

    async def _open(self):
        if self.file is None:

            async def opening():
                self.file = await asyncio.to_thread(open, self.path, "rb")

            task = live_authority.own(asyncio.create_task(opening(), name="live-media-file-open"))
            self.pending.add(task)
            await asyncio.shield(task)
            self.pending.discard(task)

    async def close(self):
        if self.pending:
            await asyncio.gather(*self.pending, return_exceptions=True)
            self.pending.clear()
        if self.file is not None:
            await self._io(self.file.close)
            self.file = None

    async def _handle_simple(self, send, send_header_only, send_pathsend):
        await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
        if send_header_only:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        await self._open()
        while True:
            chunk = await self._io(self.file.read, self.chunk_size)
            more = len(chunk) == self.chunk_size
            await send({"type": "http.response.body", "body": chunk, "more_body": more})
            if not more:
                break

    async def _handle_single_range(self, send, start, end, file_size, send_header_only):
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-range"] = f"bytes {start}-{end - 1}/{file_size}"
        headers["content-length"] = str(end - start)
        await send({"type": "http.response.start", "status": 206, "headers": headers.raw})
        if send_header_only:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        await self._open()
        await self._io(self.file.seek, start)
        while start < end:
            chunk = await self._io(self.file.read, min(self.chunk_size, end - start))
            start += len(chunk)
            await send({"type": "http.response.body", "body": chunk, "more_body": start < end})
            if not chunk:
                break

    async def _handle_multiple_ranges(self, send, ranges, file_size, send_header_only):
        from secrets import token_hex

        boundary = token_hex(13)
        content_length, header_generator = self.generate_multipart(
            ranges, boundary, file_size, self.headers["content-type"]
        )
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-type"] = f"multipart/byteranges; boundary={boundary}"
        headers["content-length"] = str(content_length)
        await send({"type": "http.response.start", "status": 206, "headers": headers.raw})
        if send_header_only:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        await self._open()
        for start, end in ranges:
            await send({"type": "http.response.body", "body": header_generator(start, end), "more_body": True})
            await self._io(self.file.seek, start)
            while start < end:
                chunk = await self._io(self.file.read, min(self.chunk_size, end - start))
                start += len(chunk)
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
                if not chunk:
                    break
            await send({"type": "http.response.body", "body": b"\r\n", "more_body": True})
        await send({"type": "http.response.body", "body": f"--{boundary}--".encode("latin-1"), "more_body": False})


class OwnedMediaResponse(Response):
    def __init__(self, response: Response, request, *, lease_seconds=None):
        super().__init__(
            status_code=response.status_code, headers=dict(response.headers), background=response.background
        )
        self.payload = _OwnedFileBody(response) if isinstance(response, FileResponse) else response
        self.request = request
        self.lease_seconds = lease_seconds

    async def __call__(self, scope, receive, send):
        # FastAPI may attach dependency BackgroundTasks after an endpoint has
        # returned an explicit OwnedMediaResponse.
        self.payload.background = self.background
        descriptor = await _descriptor(self.request)
        if self.lease_seconds is not None:
            # Capture once at this stream's admission. Coordinator refreshes
            # can renew freshness, never this independent absolute deadline.
            descriptor = replace(descriptor, expires=min(descriptor.expires, time.monotonic() + self.lease_seconds))
        handle = live_authority.register(descriptor)
        viewer = MediaViewer(handle)
        viewer.outer_task = scope.setdefault("state", {}).get("live_media_outer_task")
        scope.setdefault("state", {})["live_media_viewer"] = viewer
        handle.on_retire = viewer.retired
        if isinstance(self.payload, _OwnedFileBody):
            viewer.releases.append(self.payload.close)

        async def body_send(message):
            if message["type"] == "http.response.start":
                if not handle.deliverable():
                    raise HTTPException(503, "Authentication temporarily unavailable")
                viewer.headers_started = True
            if message["type"] == "http.response.body" and not handle.deliverable():
                raise _DeliveryStopped()
            await send(message)

        async def body():
            try:
                await live_authority.admit(handle)
                if isinstance(self.payload, _MjpegBody):
                    await self.payload.run(viewer, scope, receive, body_send)
                else:
                    # Never let pathsend bypass the chunk boundary.
                    local_scope = dict(scope)
                    local_scope["extensions"] = {
                        k: v for k, v in scope.get("extensions", {}).items() if k != "http.response.pathsend"
                    }
                    await self.payload(local_scope, receive, body_send)
            finally:
                viewer.request_cleanup()

        async def disconnected():
            while True:
                if (await receive())["type"] == "http.disconnect":
                    return

        viewer.body = live_authority.own(asyncio.create_task(body(), name="live-media-body"))
        disconnect = live_authority.own(asyncio.create_task(disconnected(), name="live-media-disconnect"))
        try:
            done, _ = await asyncio.wait((viewer.body, disconnect), return_when=asyncio.FIRST_COMPLETED)
            if disconnect in done:
                viewer.disconnected()
            try:
                await asyncio.shield(viewer.body)
            except asyncio.CancelledError:
                # Controller-initiated retirement/disconnect is EOF. A caller
                # cancellation still propagates after requesting owned cleanup.
                if not handle.retired and disconnect not in done:
                    raise
                if handle.retired and not viewer.headers_started and not viewer.disconnect_seen:
                    raise HTTPException(503, "Authentication temporarily unavailable") from None
        finally:
            viewer.cancel_body()
            disconnect.cancel()
            try:
                with anyio.CancelScope(shield=True):
                    await asyncio.shield(asyncio.gather(disconnect, return_exceptions=True))
                await _join_cleanup(viewer)
            finally:
                if not scope["state"].get("live_media_outer"):
                    viewer.finish_delivery()


def mjpeg_response(request, factory, *, lease_seconds=None):
    return OwnedMediaResponse(_MjpegBody(factory), request, lease_seconds=lease_seconds)


def bind_printer(request, printer, access_guard=None):
    binding = {"printer_id": printer.id, "printer_identity": printer.connector_identity}
    if access_guard is not None:
        binding.update(key_hash=access_guard.identity.key_hash, namespace=access_guard.namespace)
    request.state.live_media_binding = binding


def bind_camera(request, camera):
    request.state.live_media_binding = {
        "resource_kind": "camera",
        "resource_id": camera.id,
        "resource_stamp": (camera.created_at, camera.camera_type, camera.url, camera.snapshot_url, camera.enabled),
    }


async def capture_initial_media_binding(request: Request):
    """After auth dependencies, before slow preparation: pin the observed row."""
    descriptor = await _descriptor(request)
    binding = dict(getattr(request.state, "live_media_binding", {}))
    for name in ("printer_id", "printer_identity", "resource_kind", "resource_id", "resource_stamp"):
        value = getattr(descriptor, name, None)
        if value is not None:
            binding.setdefault(name, value)
    request.state.live_media_binding = binding


def install_live_media_routes(app, prefix=None):
    """Wrap only the declared visual-media routes after router registration."""
    if prefix is None:
        from backend.app.core.config import settings

        prefix = settings.api_prefix
    installed = set()
    for route in app.routes:
        if not isinstance(route, APIRoute) or _template(route.path, prefix) not in MEDIA_ROUTES:
            continue
        installed.add(_template(route.path, prefix))
        if getattr(route, "_live_media_installed", False):
            continue
        route.dependant.dependencies.append(
            get_parameterless_sub_dependant(
                depends=Depends(capture_initial_media_binding),
                path=route.path_format,
            )
        )
        original = route.get_route_handler()

        def wrap(handler):
            @wraps(handler)
            async def endpoint(request):
                response = await handler(request)
                if isinstance(response, OwnedMediaResponse) or response.status_code >= 400:
                    return response
                return OwnedMediaResponse(response, request)

            return endpoint

        route.app = request_response(wrap(original))
        route._live_media_installed = True
    return frozenset(installed)


class LiveMediaDeliveryMiddleware:
    """Gate at the final send, beyond BaseHTTPMiddleware rendezvous buffers."""

    def __init__(self, app, prefix=None):
        self.app = app
        if prefix is None:
            from backend.app.core.config import settings

            prefix = settings.api_prefix
        self.patterns = tuple(_pattern(prefix + path) for path in MEDIA_ROUTES)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not any(p.fullmatch(scope["path"]) for p in self.patterns):
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        state["live_media_outer"] = True
        caller = asyncio.current_task()
        initial_cancels = caller.cancelling()
        successful = False
        aborted = False

        async def final_send(message):
            nonlocal successful, aborted
            if message["type"] == "http.response.start":
                successful = message["status"] < 400
                viewer = state.get("live_media_viewer")
                if viewer is not None:
                    viewer.outer_started = True
            elif message["type"] == "http.response.body" and successful:
                viewer = state.get("live_media_viewer")
                if viewer is None or not viewer.handle.deliverable():
                    aborted = True
                    if viewer is not None:
                        viewer.handle.retire(1013)
                    raise _DeliveryStopped()
            await send(message)

        async def run_app():
            nonlocal aborted
            try:
                await self.app(scope, receive, final_send)
            except* _DeliveryStopped:
                aborted = True

        # This private ASGI owner includes the actual server-facing send.
        # Cancelling it cannot alter the caller's cancellation count, including
        # AnyIO's internal uncancel bookkeeping or simultaneous caller cancel.
        actual = live_authority.own(asyncio.create_task(run_app(), name="live-media-delivery"), cleanup=True)
        state["live_media_outer_task"] = actual

        async def finish_delivery():
            await asyncio.gather(actual, return_exceptions=True)
            viewer = state.get("live_media_viewer")
            if aborted or (viewer is not None and viewer.handle.retired):
                logger.info("Live media delivery stopped: category=authority_retired")
            if viewer is not None:
                if aborted:
                    viewer.retired()
                viewer.finish_delivery()

        try:
            try:
                await asyncio.shield(actual)
            except asyncio.CancelledError:
                viewer = state.get("live_media_viewer")
                if caller.cancelling() > initial_cancels or viewer is None or not viewer.outer_cancel_sent:
                    raise
                aborted = True
        finally:
            if not actual.done():
                actual.cancel()
            finalizer = live_authority.own(
                asyncio.create_task(finish_delivery(), name="live-media-delivery-cleanup"), cleanup=True
            )
            with anyio.CancelScope(shield=True):
                await asyncio.shield(finalizer)
