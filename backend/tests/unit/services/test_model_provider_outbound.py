"""The configured-origin transport: where a provider with an administrator-set address may send a request.

Every case drives the real transport over ``httpx.MockTransport`` and asserts on the requests that
reached the "network" — the forbidden request is the thing looked for.
"""

from __future__ import annotations

import httpx
import pytest

from backend.app.services.model_providers import outbound
from backend.app.services.model_providers.outbound import (
    GuardedTransport,
    OutboundRefused,
    OutboundTooLarge,
    PinnedOrigin,
)

BASE = "https://manyfold.lan:8443/mf"


DNS = {
    "manyfold.lan": ["192.168.1.20"],
    "m.lan": ["192.168.1.20"],
    "localhost": ["127.0.0.1"],
    "models.example.org": ["93.184.216.34"],
    "bucket.s3.example.com": ["52.216.1.1", "2600:1f18::1"],
    "elsewhere.example.com": ["93.184.216.40"],
    "rebind.example.com": ["93.184.216.34", "169.254.169.254"],
    "imds.example.com": ["169.254.169.254"],
    "inside.example.com": ["10.0.0.5"],
    "loop.example.com": ["127.0.0.1"],
    "cgnat.example.com": ["100.64.0.9"],
    "ula.example.com": ["fd00::5"],
    "mapped.example.com": ["::ffff:10.0.0.5"],
}


@pytest.fixture(autouse=True)
def scripted_dns(monkeypatch):
    """No test resolves a real name: the table above is the whole DNS."""
    asked: list[str] = []

    async def resolve(host: str, port: int) -> list[str]:
        asked.append(host)
        if host not in DNS:
            raise OSError(f"unscripted name {host}")
        return list(DNS[host])

    monkeypatch.setattr(outbound, "resolve_host", resolve, raising=False)
    return asked


class ElseStorage(dict):
    """Routes where every address nobody scripted is file storage that answers."""

    def get(self, key, default=None):
        return super().get(key, httpx.Response(200, content=b"solid"))


class Net:
    """A scripted network: ``routes`` maps a URL (without query) to a handler or a response."""

    def __init__(self, routes):
        self.routes = routes
        self.seen: list[httpx.Request] = []
        self.connected: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        # The address the transport checked for this request (its socket layer connects there).
        self.connected.append(request.extensions["pinned_address"])
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

    async def test_a_download_follows_the_hop_to_storage_without_the_token(self):
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
        base = "https://models.example.org"
        other = "https://models.example.org:9000/b/a.stl"
        net = Net({f"{base}/models/m/raw/a.stl": redirect(other), other: httpx.Response(200, content=b"x")})
        transport = transport_for(net, base)
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

    async def test_storage_may_redirect_on_and_nothing_after_the_origin_carries_the_token(self):
        storage = "https://bucket.s3.example.com/a.stl"
        final = "http://elsewhere.example.com/a.stl"
        net = Net(
            {
                f"{BASE}/models/m/raw/a.stl": redirect(storage),
                storage: redirect(final),
                final: httpx.Response(200, content=b"solid"),
            }
        )
        transport = transport_for(net)
        status, data, foreign = await body_of(transport, "/models/m/raw/a.stl", bearer="tok", foreign_hop=True)
        assert (status, data, foreign) == (200, b"solid", True)
        assert [("authorization" in request.headers) for request in net.seen] == [True, False, False]

    async def test_a_chain_longer_than_the_limit_is_refused(self):
        hop = "https://bucket.s3.example.com/hop{}"
        routes = {f"{BASE}/models/m/raw/a.stl": redirect(hop.format(0))}
        for number in range(outbound.MAX_STORAGE_HOPS + 1):
            routes[hop.format(number)] = redirect(hop.format(number + 1))
        net = Net(routes)
        transport = transport_for(net)
        with pytest.raises(OutboundRefused) as caught:
            await body_of(transport, "/models/m/raw/a.stl", bearer="tok", foreign_hop=True)
        assert caught.value.reason == "redirect_chain"
        assert len(net.seen) == 1 + outbound.MAX_STORAGE_HOPS

    async def test_a_same_origin_redirect_of_a_download_is_not_followed(self):
        net = Net({f"{BASE}/models/m/raw/a.stl": redirect(f"{BASE}/users/sign_in")})
        transport = transport_for(net)
        async with transport.stream("/models/m/raw/a.stl", bearer="tok", foreign_hop=True) as answer:
            assert (answer.status, answer.redirect) == (302, "same_origin")
        assert len(net.seen) == 1


class TestHostsThatAreAddressesInDisguise:
    @pytest.mark.parametrize(
        "host",
        [
            "2852039166",  # decimal 169.254.169.254
            "0xA9FEA9FE",
            "0251.0376.0251.0376",  # octal
            "169.254.43518",  # short form
            "169.16689662",
            "0xa9.0xfe.0xa9.0xfe",
            "0xa9.254.0251.254",  # mixed
            "127.1",
            "010.0.0.1",
        ],
    )
    def test_a_numeric_host_that_is_not_a_canonical_ipv4_is_refused(self, host):
        with pytest.raises(ValueError):
            PinnedOrigin.admit(f"http://{host}:3214", label="Manyfold URL")

    @pytest.mark.parametrize(
        "host",
        [
            "metadata.google.internal.",
            "METADATA.GOOGLE.INTERNAL..",
            "169.254.169.254.",
            "[::ffff:169.254.169.254]",
            "[::ffff:a9fe:a9fe]",
            "[fe80::1%25eth0]",
            "[fe80::1]",
            "[::]",
        ],
    )
    def test_a_denied_host_is_denied_however_it_is_spelled(self, host):
        with pytest.raises(ValueError):
            PinnedOrigin.admit(f"http://{host}", label="Manyfold URL")

    def test_a_trailing_dot_names_the_same_origin(self):
        dotted = PinnedOrigin.admit("https://Manyfold.LAN.:8443/mf", label="x")
        plain = PinnedOrigin.admit("https://manyfold.lan:8443/mf", label="x")
        assert (dotted.scheme, dotted.host, dotted.port) == (plain.scheme, plain.host, plain.port)
        assert dotted.owns(httpx.URL("https://manyfold.lan.:8443/x"))
        assert plain.owns(httpx.URL("https://MANYFOLD.lan.:8443/x"))

    def test_loopback_is_an_address_an_administrator_may_configure(self):
        assert PinnedOrigin.admit("http://[::1]:3214", label="x").host == "::1"
        assert PinnedOrigin.admit("http://127.0.0.1:3214", label="x").host == "127.0.0.1"


class TestEveryConnectionGoesToACheckedAddress:
    async def test_the_configured_name_is_resolved_once_and_that_address_is_connected(self, scripted_dns):
        net = Net({f"{BASE}/models": httpx.Response(200, json={})})
        transport = transport_for(net)
        await transport.get("/models", bearer="tok")
        assert scripted_dns == ["manyfold.lan"]
        assert net.connected == ["192.168.1.20"]
        (request,) = net.seen
        assert request.url.host == "manyfold.lan"  # the URL keeps the name: pool, SNI and Host are by name

    @pytest.mark.parametrize("base", ["http://imds.example.com", "https://rebind.example.com"])
    async def test_a_configured_name_that_resolves_to_a_metadata_address_is_never_connected(self, base):
        net = Net({})
        transport = transport_for(net, base)
        with pytest.raises(OutboundRefused) as caught:
            await transport.get("/models", bearer="tok")
        assert caught.value.reason == "address_refused"
        assert net.seen == []

    async def test_a_name_that_does_not_resolve_is_unreachable(self):
        transport = transport_for(Net({}), "https://nowhere.example.com")
        with pytest.raises(OutboundRefused) as caught:
            await transport.get("/models", bearer="tok")
        assert caught.value.reason == "unreachable"

    async def test_storage_is_connected_at_the_address_that_was_checked(self, scripted_dns):
        storage = "https://bucket.s3.example.com/a.stl"
        net = Net({f"{BASE}/models/m/raw/a.stl": redirect(storage), storage: httpx.Response(200, content=b"x")})
        transport = transport_for(net)
        await body_of(transport, "/models/m/raw/a.stl", bearer="tok", foreign_hop=True)
        assert net.connected == ["192.168.1.20", "52.216.1.1"]
        assert scripted_dns == ["manyfold.lan", "bucket.s3.example.com"]
        assert net.seen[1].url.host == "bucket.s3.example.com"


class TestStorageFollowsTheOriginsOwnRules:
    """Owner, 2026-11-17 (as upstream): BamDude is a LAN system first — storage beside Manyfold on a
    private address, and over plain http, is fetched. What is dangerous in any topology is not."""

    @pytest.mark.parametrize(
        "location",
        [
            "https://127.0.0.1/a.stl",
            "http://[::1]:9000/a.stl",
            "http://10.0.0.5:9000/a.stl",
            "https://192.168.1.21:9000/a.stl",
            "http://172.16.0.1/a.stl",
            "https://100.64.0.9/a.stl",
            "http://[fd00::5]/a.stl",
            "http://localhost:9000/a.stl",
            "https://inside.example.com/a.stl",
            "http://loop.example.com/a.stl",
            "https://cgnat.example.com/a.stl",
            "https://ula.example.com/a.stl",
            "https://mapped.example.com/a.stl",
            "http://manyfold.lan:9000/a.stl",  # MinIO on the Manyfold box itself
            "http://bucket.s3.example.com/a.stl",  # public, plain http
        ],
    )
    async def test_storage_on_the_lan_or_over_plain_http_is_fetched_without_the_token(self, location):
        net = Net(ElseStorage({f"{BASE}/models/m/raw/a.stl": redirect(location)}))
        transport = transport_for(net)
        status, data, foreign = await body_of(transport, "/models/m/raw/a.stl", bearer="tok", foreign_hop=True)
        assert (status, data, foreign) == (200, b"solid", True)
        origin, storage = net.seen
        assert origin.headers["Authorization"] == "Bearer tok"
        assert "authorization" not in storage.headers and "cookie" not in storage.headers

    @pytest.mark.parametrize(
        "location",
        [
            "https://[fe80::1]/a.stl",
            "https://[fe80::1%25eth0]/a.stl",
            "https://[::ffff:169.254.169.254]/a.stl",
            "http://169.254.169.254/latest/meta-data/",
            "https://0.0.0.0/a.stl",
            "https://224.0.0.1/a.stl",
            "https://169.254.43518/a.stl",
            "https://0251.0376.0251.0376/a.stl",
            "https://metadata.google.internal./a.stl",
            "https://rebind.example.com/a.stl",  # one public answer, one metadata answer
            "http://imds.example.com/a.stl",
        ],
    )
    async def test_a_redirect_to_a_metadata_or_link_local_address_is_never_requested(self, location):
        net = Net(ElseStorage({f"{BASE}/models/m/raw/a.stl": redirect(location)}))
        transport = transport_for(net)
        with pytest.raises(OutboundRefused):
            await body_of(transport, "/models/m/raw/a.stl", bearer="tok", foreign_hop=True)
        assert net.urls() == [f"{BASE}/models/m/raw/a.stl"]

    async def test_a_way_back_to_the_configured_origin_is_taken_without_the_token(self):
        """origin -> storage -> origin: the last request is storage's, so it carries nothing."""
        storage = "https://bucket.s3.example.com/a.stl"
        net = Net(
            {
                f"{BASE}/models/m/raw/a.stl": redirect(storage),
                storage: redirect(f"{BASE}/models/m/raw/b.stl"),
                f"{BASE}/models/m/raw/b.stl": httpx.Response(200, content=b"x"),
            }
        )
        transport = transport_for(net)
        _status, _data, foreign = await body_of(transport, "/models/m/raw/a.stl", bearer="tok", foreign_hop=True)
        assert foreign is True and len(net.seen) == 3
        assert "authorization" not in net.seen[2].headers

    async def test_a_same_origin_redirect_that_then_points_at_storage_is_not_followed_at_all(self):
        """origin -> origin -> storage: the first hop already stays unfollowed."""
        net = Net(
            {
                f"{BASE}/models/m/raw/a.stl": redirect(f"{BASE}/models/m/raw/b.stl"),
                f"{BASE}/models/m/raw/b.stl": redirect("https://bucket.s3.example.com/a.stl"),
            }
        )
        transport = transport_for(net)
        status, _data, foreign = await body_of(transport, "/models/m/raw/a.stl", bearer="tok", foreign_hop=True)
        assert (status, foreign) == (302, False) and len(net.seen) == 1


class TestAddressesThatCarryAnotherAddress:
    """Review MB-M1: IPv6 forms that embed an IPv4 address, or that ``is_global`` wrongly calls public."""

    @pytest.mark.parametrize(
        "address",
        [
            "::169.254.169.254",  # IPv4-compatible
            "::a9fe:a9fe",
            "::ffff:0:169.254.169.254",  # SIIT (RFC 2765)
            "::ffff:0:a9fe:a9fe",
            "64:ff9b:1::a9fe:a9fe",  # local-use NAT64 (RFC 8215): the embedding is the operator's
            "64:ff9b:1:abc::7f00:1",
            "2001:0:a9fe:a9fe::1",  # Teredo, metadata as its server
            "2001:0:808:808:0:0:5601:5601",  # Teredo, client 169.254.169.254 (stored inverted)
            "fec0::1",  # deprecated site-local
            "feff::1",
        ],
    )
    def test_it_is_never_connected(self, address):
        assert outbound.address_refusal(address) is not None

    @pytest.mark.parametrize(
        "address", ["::127.0.0.1", "::7f00:1", "::10.0.0.5", "::ffff:0:127.0.0.1", "::8.8.8.8", "::ffff:0:8.8.8.8"]
    )
    def test_an_embedded_address_is_judged_as_the_address_it_carries(self, address):
        # Loopback or private, embedded or not, is allowed — for the origin and for storage alike.
        assert outbound.address_refusal(address) is None

    @pytest.mark.parametrize("address", ["::1", "fd00::1", "2606:4700::1111", "::ffff:8.8.8.8", "8.8.8.8"])
    def test_ordinary_addresses_are_judged_as_before(self, address):
        assert outbound.address_refusal(address) is None

    @pytest.mark.parametrize(
        "url",
        [
            "http://[::169.254.169.254]/",
            "http://[64:ff9b:1::a9fe:a9fe]/",
            "http://[2001:0:a9fe:a9fe::1]/",
            "http://[fec0::1]/",
        ],
    )
    def test_such_an_address_is_not_a_configured_origin(self, url):
        with pytest.raises(ValueError):
            PinnedOrigin.admit(url, label="Manyfold URL")

    @pytest.mark.parametrize(
        "location", ["https://[::169.254.169.254]/x", "https://[fec0::1]/x", "https://[64:ff9b:1::a9fe:a9fe]/x"]
    )
    async def test_storage_at_such_an_address_is_refused_without_a_request(self, location):
        net = Net({f"{BASE}/models/m/raw/a.stl": redirect(location)})
        transport = transport_for(net)
        with pytest.raises(OutboundRefused) as caught:
            await body_of(transport, "/models/m/raw/a.stl", bearer="tok", foreign_hop=True)
        assert caught.value.reason == "foreign_refused"
        assert net.urls() == [f"{BASE}/models/m/raw/a.stl"]


class TestAnAddressTheClientCannotSendIsNotAdmitted:
    """Review MB-M2: what is saved must be what the HTTP client will connect for — or saving fails."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://１２７.０.０.１/",  # full-width digits: a resolver's 127.0.0.1, unparsable to the client
            "http://①②⑦.0.0.1/",
            "http://straße.de/",  # IDNA 2003 says strasse.de, the client sends xn--strae-oqa.de
        ],
    )
    def test_it_is_refused_with_a_sentence_for_the_form(self, url):
        with pytest.raises(ValueError) as caught:
            PinnedOrigin.admit(url, label="Manyfold URL")
        assert str(caught.value).startswith("Manyfold URL ")

    @pytest.mark.parametrize(
        ("url", "host"),
        [
            ("http://Manyfold.LAN:3214", "manyfold.lan"),
            ("http://xn--strae-oqa.de/", "xn--strae-oqa.de"),
            ("http://[FD12:0::20]:3214", "fd12::20"),
            ("http://192.168.1.20:3214/sub", "192.168.1.20"),
            ("https://Manyfold.Example.COM./lib", "manyfold.example.com"),  # no dot reaches Host or SNI
        ],
    )
    async def test_what_is_admitted_is_what_the_client_connects_for(self, url, host):
        origin = PinnedOrigin.admit(url, label="Manyfold URL")
        assert origin.host == host
        sent = httpx.URL(f"{origin.base}/models")
        assert sent.raw_host.decode() == origin.host
