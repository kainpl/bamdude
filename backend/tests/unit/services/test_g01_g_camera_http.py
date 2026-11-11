"""G01 G: camera URL and HTTP-hop policy on real loopback transports only."""

import asyncio
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web

from backend.app.services import external_camera as ec


@pytest.mark.parametrize(
    "host",
    [
        "metadata.google.internal.",
        "metadata.goog",
        "metadata.google.",
        "169.254.169.254.",
        "100.100.100.200",
        "2852039166",
        "0xA9FEA9FE",
        "0251.0376.0251.0376",
        "169.254.43518",
        "%31%36%39.254.169.254",
        "[fd00:ec2::254]",
        "[::ffff:100.100.100.200]",
        "169.254.169.0254",
        ":80",
        "example.com:0",
        "example.com:65536",
        "example.com:bad",
        "example.com:",
    ],
)
def test_ambiguous_or_metadata_target_is_refused_without_network(host):
    assert ec._sanitize_camera_url(f"http://{host}/frame") is None


@pytest.mark.parametrize(
    "url",
    [
        "http://camera.local/\nframe",
        "http://camera.local/\tframe",
        " http://camera.local/frame",
        "http://camera.local\\@169.254.169.254/frame",
        "http://u:p@extra@camera.local/frame",
    ],
)
def test_parser_ambiguous_authority_or_controls_refused(url):
    assert ec._sanitize_camera_url(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:1984/api/frame.jpeg?src=cam",
        "http://localhost:1984/frame",
        "https://192.168.1.2/frame",
        "http://10.1.2.3/frame",
        "http://172.16.0.1/frame",
        "http://[::1]:1984/frame",
        "http://[fd12::123]/frame",
        "rtsp://camera.local:554/live",
        "http://user%40name:p%3Ass@[::1]:1984/api/stream.mjpeg?src=cam",
    ],
)
def test_intentional_camera_lan_and_go2rtc_remain_supported(url):
    assert ec._sanitize_camera_url(url) == url


@asynccontextmanager
async def endpoint(handler):
    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", ["userinfo", "default_auth", "header"])
async def test_same_origin_keeps_credentials_then_cross_origin_strips_all(credential):
    seen = []

    async def other(request):
        seen.append(("other", request.headers.get("Authorization"), request.headers.get("Cookie")))
        return web.Response(text="ok")

    async with endpoint(other) as target:

        async def origin(request):
            seen.append((request.path, request.headers.get("Authorization"), request.headers.get("Cookie")))
            if request.path == "/start":
                response = web.Response(status=302, headers={"Location": "/same"})
                response.set_cookie("camera_session", "cookie-canary")
                return response
            # A peer-supplied userinfo must not become a new credential source.
            return web.Response(status=302, headers={"Location": target.replace("://", "://injected:canary@")})

        async with endpoint(origin) as source:
            kwargs = {"cookie_jar": aiohttp.CookieJar(unsafe=True)}
            if credential == "default_auth":
                kwargs["auth"] = aiohttp.BasicAuth("saved", "password-canary")
            if credential == "header":
                kwargs["headers"] = {"Authorization": "Bearer header-canary"}
            if credential == "userinfo":
                source = source.replace("://", "://saved:password-canary@")
            async with (
                aiohttp.ClientSession(**kwargs) as session,
                ec._validated_camera_get(session, source + "/start") as response,
            ):
                assert await response.text() == "ok"
    assert seen[0][1] and seen[1][1] == seen[0][1]
    assert "cookie-canary" in seen[1][2]
    assert seen[-1] == ("other", None, None)


@pytest.mark.asyncio
async def test_cross_origin_return_never_restores_original_credentials():
    final = []
    source = ""

    async def middle(request):
        return web.Response(status=302, headers={"Location": source + "/final"})

    async with endpoint(middle) as other:

        async def origin(request):
            if request.path == "/start":
                return web.Response(status=302, headers={"Location": other})
            final.append((request.headers.get("Authorization"), request.headers.get("Cookie")))
            return web.Response(text="ok")

        async with (
            endpoint(origin) as source,
            aiohttp.ClientSession(headers={"Authorization": "Bearer canary"}) as session,
            ec._validated_camera_get(session, source + "/start") as response,
        ):
            assert await response.text() == "ok"
    assert final == [(None, None)]


@pytest.mark.asyncio
async def test_relative_redirect_loop_stops_before_second_request_to_same_target():
    calls = []

    async def loop(request):
        calls.append(request.path)
        return web.Response(status=302, headers={"Location": "/loop"})

    async with endpoint(loop) as source, aiohttp.ClientSession() as session:
        with pytest.raises(ValueError):
            async with ec._validated_camera_get(session, source + "/loop"):
                pytest.fail("loop admitted")
    assert calls == ["/loop"]


@pytest.mark.asyncio
async def test_initial_restricted_url_is_checked_before_any_get():
    class Session:
        def get(self, *args, **kwargs):
            pytest.fail("restricted initial target reached network")

    with pytest.raises(ValueError):
        async with ec._validated_camera_get(Session(), "http://2852039166/frame"):
            pytest.fail("restricted target admitted")


@pytest.mark.asyncio
async def test_one_header_deadline_covers_all_redirect_hops():
    calls = []

    async def delayed(request):
        calls.append(request.path)
        await asyncio.sleep(0.07)
        return web.Response(status=302, headers={"Location": "/" + str(len(calls))})

    async with endpoint(delayed) as source, aiohttp.ClientSession() as session:
        with pytest.raises(TimeoutError):
            async with ec._validated_camera_get(session, source + "/start", timeout=0.12):
                pytest.fail("deadline admitted")
    assert len(calls) == 2
