"""GA3: bounded reader configuration survives redirects; diagnostics omit URL secrets."""

from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web

from backend.app.services import external_camera as camera
from backend.app.services.camera_http import _validated_camera_get


@asynccontextmanager
async def endpoint(handler):
    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_cross_origin_anonymous_reader_retains_configured_buffer_limits():
    async def final(request):
        assert "Authorization" not in request.headers
        assert request.headers["Accept-Encoding"] == "identity"
        return web.Response(body=b"still", headers={"Content-Encoding": "gzip"})

    async with endpoint(final) as destination:

        async def redirect(request):
            raise web.HTTPFound(destination)

        async with (
            endpoint(redirect) as source,
            aiohttp.ClientSession(
                read_bufsize=64 * 1024, auto_decompress=False, headers={"Accept-Encoding": "identity"}
            ) as session,
            _validated_camera_get(session, source.replace("://", "://user:password@")) as response,
        ):
            assert response.content.get_read_buffer_limits() == (64 * 1024, 128 * 1024)
            assert await response.read() == b"still"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authority,host",
    [("camera.example:8080", "camera.example:8080"), ("[::1]:8081", "[::1]:8081")],
)
async def test_snapshot_warning_projects_only_configured_host(monkeypatch, caplog, authority, host):
    @asynccontextmanager
    async def refused(*args, **kwargs):
        raise aiohttp.ClientConnectionError("PRIVATE_EXCEPTION_TEXT")
        yield  # pragma: no cover

    monkeypatch.setattr(camera, "_validated_camera_get", refused)
    monkeypatch.setattr(camera, "_snapshot_warnings", {})
    assert (
        await camera._capture_snapshot(f"http://USER:P@SS WORD@{authority}/SECRET_PATH?TOKEN=SECRET_QUERY", 1) is None
    )
    assert f"sample_host={host}" in caplog.text
    assert "events=1" in caplog.text
    for secret in ("USER", "SS WORD", "SECRET_PATH", "SECRET_QUERY", "PRIVATE_EXCEPTION_TEXT"):
        assert secret not in caplog.text


def test_warning_aggregation_is_by_category_with_representative_host(monkeypatch, caplog):
    now = [0.0]
    monkeypatch.setattr(camera.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(camera, "_snapshot_warnings", {})
    for index in range(100):
        camera._snapshot_warning("Snapshot encoded-payload byte budget exhausted", source_host=f"camera{index}.example")
    now[0] = 61
    camera._snapshot_warning("Snapshot encoded-payload byte budget exhausted", source_host="current.example")
    assert len(camera._snapshot_warnings) == 1
    assert len(caplog.records) == 2
    assert "events=100; sample_host=current.example" in caplog.records[-1].message
    assert "suppressed since prior report: 99" in caplog.records[-1].message


@pytest.mark.asyncio
async def test_real_byte_capacity_warning_omits_url_secrets_and_releases_budget(monkeypatch, caplog):
    budget = camera.SnapshotByteBudget(16)
    monkeypatch.setattr(camera, "_SNAPSHOT_BYTE_BUDGET", budget)
    monkeypatch.setattr(camera, "_snapshot_warnings", {})

    async def handler(request):
        return web.Response(body=b"\xff\xd8\xff" + b"x" * 64, content_type="image/jpeg")

    async with endpoint(handler) as url:
        url = url.replace("://", "://CANARYUSER:CANARY@PASS WORD@") + "/SECRET_PATH?token=SECRET_QUERY"
        assert await camera._capture_snapshot(url, 1) is None
    # The synthetic camera's own access logger records its received URL; this
    # assertion concerns the BamDude capture diagnostic, not that peer's logs.
    diagnostics = "\n".join(record.getMessage() for record in caplog.records if record.name == camera.__name__)
    assert "Snapshot encoded-payload byte budget exhausted" in diagnostics
    assert "events=1; sample_host=127.0.0.1:" in diagnostics
    assert budget.used_bytes == budget.peak_bytes == 0
    for secret in ("CANARYUSER", "PASS WORD", "SECRET_PATH", "SECRET_QUERY"):
        assert secret not in diagnostics
