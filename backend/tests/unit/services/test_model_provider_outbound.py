"""The configured-origin transport: where a provider with an administrator-set address may send a request.

Every case drives the real transport over ``httpx.MockTransport`` and asserts on the requests that
reached the "network" — the forbidden request is the thing looked for.
"""

from __future__ import annotations

import httpx
import pytest

from backend.app.services.model_providers.outbound import (
    GuardedTransport,
    OutboundRefused,
    OutboundTooLarge,
    PinnedOrigin,
)

BASE = "https://manyfold.lan:8443/mf"


class Net:
    """A scripted network: ``routes`` maps a URL (without query) to a handler or a response."""

    def __init__(self, routes):
        self.routes = routes
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        key = str(request.url.copy_with(query=None))
        answer = self.routes.get(key)
        if answer is None:
            return httpx.Response(599, text=f"unscripted {key}")
        return answer(request) if callable(answer) else answer

    def urls(self) -> list[str]:
        return [str(request.url) for request in self.seen]


def transport_for(net: Net, base: str = BASE) -> GuardedTransport:
    return GuardedTransport(
        PinnedOrigin.admit(base, label="Manyfold URL"), client=httpx.AsyncClient(transport=httpx.MockTransport(net))
    )


def redirect(location: str, status: int = 302) -> httpx.Response:
    return httpx.Response(status, headers={"Location": location})


async def body_of(transport: GuardedTransport, path: str, **kwargs) -> tuple[int, bytes, bool]:
    async with transport.stream(path, **kwargs) as answer:
        data = b"".join([chunk async for chunk in answer.chunks(1024 * 1024)])
        return answer.status, data, answer.foreign


class TestAdmission:
    @pytest.mark.parametrize(
        "url",
        [
            "http://192.168.1.20:3214",
            "http://localhost:3214/",
            "https://models.example.org/manyfold",
            "http://[fd12::20]:3214",
        ],
    )
    def test_a_lan_or_public_address_is_admitted(self, url):
        origin = PinnedOrigin.admit(url, label="Manyfold URL")
        assert origin.base == url.rstrip("/")

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://manyfold.lan",
            "https://",
            "https://user:pw@manyfold.lan",
            "https://manyfold.lan/?x=1",
            "https://manyfold.lan/#frag",
            "http://169.254.169.254",
            "http://169.254.10.10",
            "http://metadata.google.internal",
            "http://2130706433",
            "http://0x7f000001",
            "http://[::ffff:169.254.169.254]",
            "http://[64:ff9b::a9fe:a9fe]",
            "http://0.0.0.0:3214",
            "http://224.0.0.1",
            "https://manyfold.lan\\@evil.example",
            "https://manyfold.lan/a/../b",
            " https://manyfold.lan",
            "https://many fold.lan",
        ],
    )
    def test_a_dangerous_or_ambiguous_address_is_refused(self, url):
        with pytest.raises(ValueError):
            PinnedOrigin.admit(url, label="Manyfold URL")

    def test_the_default_port_is_part_of_the_origin(self):
        assert PinnedOrigin.admit("https://m.lan", label="x").port == 443
        assert PinnedOrigin.admit("http://m.lan", label="x").port == 80


class TestRequestsAreBuiltFromTheBase:
    async def test_a_path_is_sent_under_the_configured_base_with_the_token(self):
        net = Net({f"{BASE}/models": httpx.Response(200, json={"ok": True})})
        transport = transport_for(net)
        answer = await transport.get("/models", params={"page": 2}, bearer="tok", accept="application/x")
        assert answer.status == 200 and answer.json() == {"ok": True}
        (request,) = net.seen
        assert str(request.url) == f"{BASE}/models?page=2"
        assert request.headers["Authorization"] == "Bearer tok"
        assert request.headers["Accept"] == "application/x"
        assert request.headers["User-Agent"].startswith("BamDude/")
        assert "cookie" not in request.headers

    @pytest.mark.parametrize(
        "path",
        [
            "models",
            "//evil.example/x",
            "/models/../../admin",
            "/models/%2e%2e/admin",
            "/models/a?x=1",
            "/models/a#f",
            "/models/a\\b",
            "/models/a\nb",
            "https://evil.example/x",
        ],
    )
    async def test_a_path_that_could_leave_the_base_never_reaches_the_network(self, path):
        net = Net({})
        transport = transport_for(net)
        with pytest.raises(OutboundRefused):
            await transport.get(path, bearer="tok")
        assert net.seen == []

    async def test_cookies_an_answer_set_are_never_sent_back(self):
        net = Net({f"{BASE}/models": httpx.Response(200, json={}, headers={"Set-Cookie": "_session=abc; Path=/"})})
        transport = transport_for(net)
        await transport.get("/models", bearer="tok")
        await transport.get("/models", bearer="tok")
        assert all("cookie" not in request.headers for request in net.seen)

    async def test_an_answer_larger_than_its_cap_is_refused(self):
        net = Net({f"{BASE}/models": httpx.Response(200, content=b"x" * 4096)})
        transport = transport_for(net)
        with pytest.raises(OutboundTooLarge):
            await transport.get("/models", bearer="tok", cap=1024)

    async def test_a_declared_length_over_the_cap_is_refused_before_the_body_is_read(self):
        def big(_request):
            return httpx.Response(200, headers={"Content-Length": "999999999"}, content=b"")

        net = Net({f"{BASE}/models/m/raw/a.stl": big})
        transport = transport_for(net)
        with pytest.raises(OutboundTooLarge):
            async with transport.stream("/models/m/raw/a.stl", bearer="tok") as answer:
                async for _chunk in answer.chunks(1024):
                    pass

    async def test_a_network_failure_is_reported_without_the_token(self):
        def boom(request):
            raise httpx.ConnectError("no route", request=request)

        transport = transport_for(Net({f"{BASE}/models": boom}))
        with pytest.raises(OutboundRefused) as caught:
            await transport.get("/models", bearer="secret-token")
        assert caught.value.reason == "unreachable"
        assert "secret-token" not in str(caught.value)


class TestRedirects:
    async def test_a_json_request_follows_no_redirect_and_reports_its_kind(self):
        net = Net(
            {
                f"{BASE}/models": redirect("/mf/users/sign_in"),
                f"{BASE}/users/sign_in": httpx.Response(200, text="<html>"),
            }
        )
        transport = transport_for(net)
        answer = await transport.get("/models", bearer="tok")
        assert (answer.status, answer.redirect) == (302, "same_origin")
        assert len(net.seen) == 1

    async def test_a_redirect_to_https_on_the_same_host_is_reported_as_a_scheme_upgrade(self):
        net = Net({"http://manyfold.lan/oauth/token": redirect("https://manyfold.lan/oauth/token", 301)})
        transport = transport_for(net, "http://manyfold.lan")
        answer = await transport.post_form("/oauth/token", {"a": "b"})
        assert (answer.status, answer.redirect) == (301, "scheme_upgrade")
        assert len(net.seen) == 1

    async def test_a_download_follows_one_hop_to_storage_without_the_token(self):
        storage = "https://bucket.s3.example.com/k/a%20b.stl"
        signed = f"{storage}?X-Amz-Signature=abc%2Fdef&X-Amz-Expires=900"
        net = Net({f"{BASE}/models/m/raw/a.stl": redirect(signed), storage: httpx.Response(200, content=b"solid")})
        transport = transport_for(net)
        status, data, foreign = await body_of(transport, "/models/m/raw/a.stl", bearer="tok", foreign_hop=True)
        assert (status, data, foreign) == (200, b"solid", True)
        first, second = net.seen
        assert first.headers["Authorization"] == "Bearer tok"
        assert "authorization" not in second.headers and "cookie" not in second.headers
        assert str(second.url) == signed  # the signed query reaches storage byte for byte

    async def test_another_port_of_the_same_host_is_another_origin(self):
        other = "https://manyfold.lan:9000/b/a.stl"
        net = Net({f"{BASE}/models/m/raw/a.stl": redirect(other), other: httpx.Response(200, content=b"x")})
        transport = transport_for(net)
        _status, _data, foreign = await body_of(transport, "/models/m/raw/a.stl", bearer="tok", foreign_hop=True)
        assert foreign is True
        assert "authorization" not in net.seen[1].headers

    async def test_a_download_without_the_hop_allowed_does_not_leave_the_origin(self):
        net = Net({f"{BASE}/models/m/raw/a.stl": redirect("https://bucket.s3.example.com/a.stl")})
        transport = transport_for(net)
        status, _data, _foreign = await body_of(transport, "/models/m/raw/a.stl", bearer="tok")
        assert status == 302 and len(net.seen) == 1

    @pytest.mark.parametrize(
        "location",
        [
            "http://169.254.169.254/latest/meta-data/",
            "https://169.254.169.254/latest/meta-data/",
            "https://metadata.google.internal/computeMetadata/v1/",
            "https://[fd00:ec2::254]/",
            "https://2852039166/",
            "http://bucket.s3.example.com/a.stl",  # https -> http
            "https://user:pw@bucket.s3.example.com/a.stl",
            "ftp://bucket.s3.example.com/a.stl",
            "https://bucket.s3.example.com/a\\b",
            "https://bucket.s3.example.com/a\r\nX-Evil: 1",
            " https://bucket.s3.example.com/a.stl",
            "",
        ],
    )
    async def test_a_forbidden_storage_address_is_never_requested(self, location):
        net = Net(
            {f"{BASE}/models/m/raw/a.stl": lambda _r: httpx.Response(302, headers=[(b"Location", location.encode())])}
        )
        transport = transport_for(net)
        with pytest.raises(OutboundRefused):
            await body_of(transport, "/models/m/raw/a.stl", bearer="tok", foreign_hop=True)
        assert net.urls() == [f"{BASE}/models/m/raw/a.stl"]

    async def test_plain_http_storage_is_followed_only_from_a_plain_http_base(self):
        base = "http://192.168.1.20:3214"
        minio = "http://192.168.1.21:9000/b/a.stl"
        net = Net({f"{base}/models/m/raw/a.stl": redirect(minio), minio: httpx.Response(200, content=b"x")})
        transport = transport_for(net, base)
        status, data, foreign = await body_of(transport, "/models/m/raw/a.stl", bearer="tok", foreign_hop=True)
        assert (status, data, foreign) == (200, b"x", True)

    async def test_storage_may_not_redirect_again(self):
        storage = "https://bucket.s3.example.com/a.stl"
        net = Net(
            {
                f"{BASE}/models/m/raw/a.stl": redirect(storage),
                storage: redirect("https://elsewhere.example.com/a.stl"),
            }
        )
        transport = transport_for(net)
        with pytest.raises(OutboundRefused) as caught:
            await body_of(transport, "/models/m/raw/a.stl", bearer="tok", foreign_hop=True)
        assert caught.value.reason == "redirect_chain"
        assert len(net.seen) == 2

    async def test_a_same_origin_redirect_of_a_download_is_not_followed(self):
        net = Net({f"{BASE}/models/m/raw/a.stl": redirect(f"{BASE}/users/sign_in")})
        transport = transport_for(net)
        async with transport.stream("/models/m/raw/a.stl", bearer="tok", foreign_hop=True) as answer:
            assert (answer.status, answer.redirect) == (302, "same_origin")
        assert len(net.seen) == 1
