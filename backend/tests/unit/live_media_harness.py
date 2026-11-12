"""ASGI transport fixture for viewer-owner tests; never opens physical cameras."""

import asyncio
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from backend.app.services import live_media


class Handle:
    def __init__(self, descriptor):
        self.descriptor = descriptor
        self.admitted = False
        self.retired = False
        self.on_retire = None
        self.stop = asyncio.Event()

    def deliverable(self):
        return self.admitted and not self.retired

    def retire(self, code=1013):
        self.retired = True
        self.stop.set()
        if self.on_retire:
            self.on_retire(code)


class Runtime:
    def __init__(self):
        self.handles, self.tasks, self.cleanups = set(), set(), set()

    def register(self, descriptor):
        handle = Handle(descriptor)
        self.handles.add(handle)
        return handle

    async def admit(self, handle):
        handle.admitted = True

    def own(self, task, *, cleanup=False):
        group = self.cleanups if cleanup else self.tasks
        group.add(task)
        task.add_done_callback(group.discard)
        return task

    def release(self, handle):
        self.handles.discard(handle)


@pytest.fixture
def media_runtime(monkeypatch):
    runtime = Runtime()
    monkeypatch.setattr(live_media, "live_authority", runtime)

    async def descriptor(_request):
        return SimpleNamespace()

    monkeypatch.setattr(live_media, "_descriptor", descriptor)
    return runtime


def request(path="/api/v1/printers/1/camera/stream", *, headers=(), method="GET"):
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": list(headers),
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 123),
            "state": {},
        }
    )


def serve(response, req):
    chunks = asyncio.Queue()
    disconnect = asyncio.Event()

    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            chunks.put_nowait(message)

    task = asyncio.create_task(response(req.scope, receive, send))
    return task, chunks, disconnect
