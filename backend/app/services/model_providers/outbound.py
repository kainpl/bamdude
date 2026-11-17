"""The transport of a model provider whose address an administrator configured.

MakerWorld's destinations are constants of the code (``thumbnail_hosts`` /
``download_hosts``). A self-hosted source (Manyfold) lives at an address typed
into a settings form, usually on the LAN — a second, explicitly named
destination mode, **the configured origin**:

* The address is admitted once (:meth:`PinnedOrigin.admit`): http or https, a
  host, no userinfo / query / fragment, no control characters. Loopback and
  private addresses are allowed on purpose — the administrator set the address
  and a home server is the normal topology. Cloud-metadata addresses and names,
  link-local, multicast, unspecified and numeric-encoded hosts are refused under
  any topology.
* Every request is built from that base plus a path the caller composed from
  validated ids — a URL found in a response is never fetched. The bearer token
  is attached here and only to the pinned origin (scheme + host + port).
* A host has ONE spelling (:func:`normalise_host`): lower case, no trailing
  dot, IDNA; a host of numeric parts must be a canonical dotted quad.
* Every connection goes to an address that was checked: the name is resolved
  here, EVERY answer is tested (:func:`address_refusal`), and the socket is
  opened to the tested address by the transport's own socket layer — no second
  resolution between check and connect. The request keeps the host NAME, so
  the connection pool, ``Host``, SNI and certificate verification are by name.
* The pin fails CLOSED. The HTTP layer is a pool this module owns, built from
  httpcore's public API (``_PinnedHTTP``) — never a private attribute of the
  client library. A request without a pin opens no socket; an answer is accepted
  only from a connection our socket layer opened for that name; and the first
  use in a process proves the mechanism with a sentinel over stub sockets
  (``_prove_pinning``) — a failed proof switches the transport off until a
  restart (``transport_disabled``), it never falls back to an ordinary client.
* No redirect is followed, with ONE exception a caller asks for by name
  (``foreign_hop=True``, file bytes only): one hop to another origin — object
  storage behind the source — sent without the token and without cookies,
  https only, to PUBLIC addresses only (loopback, private, link-local,
  carrier-grade NAT, unique-local and metadata addresses are refused wherever
  the configured origin lives), and that answer may not redirect again.
* Bodies are read through :meth:`GuardedAnswer.chunks` with a byte cap counted on
  the bytes of the WIRE: every request asks for ``Accept-Encoding: identity`` and
  an answer that carries a ``Content-Encoding`` anyway is refused unread — nothing
  is ever decompressed here.

Provider code holds a :class:`GuardedTransport`, never an HTTP client of its
own (``tests/unit/test_model_providers_fetch_only_through_the_guard.py``).

A proxy named by the environment is NOT used: it would resolve names itself.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import certifi
import httpcore
import httpx

from backend.app.api.routes._url_safety import CLOUD_METADATA_IPS, assert_safe_lan_service_url, unwrap_ipv4_mapped
from backend.app.core.config import APP_VERSION
from backend.app.services.model_providers.base import ProviderUnavailableError

logger = logging.getLogger(__name__)

USER_AGENT = f"BamDude/{APP_VERSION} (+https://github.com/kainpl/bamdude)"

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
_BROADCAST = ipaddress.IPv4Address("255.255.255.255")
_NUMERIC_PART = re.compile(r"0x[0-9a-f]*|[0-9]+", re.I)
#: connect / pause-between-chunks budgets; a whole-request deadline is the caller's.
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
_JSON_CAP = 4 * 1024 * 1024


class OutboundRefused(ProviderUnavailableError):
    """The transport would not send, or could not complete, a request. ``reason`` is a closed code."""

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


class OutboundTooLarge(OutboundRefused):
    def __init__(self, cap: int):
        super().__init__(f"The answer is larger than the {cap} byte limit", "too_large")
        self.cap = cap


def _has_unsafe_text(value: str) -> bool:
    return any(
        ord(char) < 32 or 127 <= ord(char) <= 159 or char == "\\" or (char.isspace() and char != " ") for char in value
    )


def normalise_host(raw: str | None, label: str) -> str:
    """The ONE spelling of a host every check and comparison uses.

    Lower case, no trailing dots (``host.`` is ``host`` to a resolver), IDNA for a non-ASCII name, the
    canonical text of an IPv6 literal. A host made only of numeric parts must be a canonical dotted
    quad: resolvers also read ``2852039166``, ``0xA9FEA9FE``, ``0251.0376.0251.0376`` and
    ``169.254.43518`` as addresses, and Python's ``ipaddress`` reads none of them.
    """
    host = (raw or "").lower().rstrip(".")
    if not host or "%" in host or any(char.isspace() for char in host):
        raise ValueError(f"{label} must include a plain hostname")
    if ":" in host:
        try:
            return str(ipaddress.IPv6Address(host))
        except ValueError as exc:
            raise ValueError(f"{label} is not a valid address") from exc
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError(f"{label} is not a valid host name") from exc
    parts = host.split(".")
    if any(not part for part in parts):
        raise ValueError(f"{label} is not a valid host name")
    if all(_NUMERIC_PART.fullmatch(part) for part in parts):
        try:
            canonical = str(ipaddress.IPv4Address(host))
        except ValueError:
            canonical = None
        if canonical != host:
            raise ValueError(f"{label} must write an IP address as four decimal numbers")
    return host


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def address_refusal(text: str, *, public_only: bool) -> str | None:
    """Why a connection to this ADDRESS is refused, or None.

    Always: cloud metadata, link-local, multicast, unspecified, broadcast — also when carried inside
    an IPv4-mapped, 6to4 or NAT64 address. ``public_only`` (the storage hop) adds everything that is
    not a public address: loopback, private, carrier-grade NAT, unique-local, reserved — and every
    translation prefix, which no storage service lives behind.
    """
    try:
        address = unwrap_ipv4_mapped(ipaddress.ip_address(text.split("%", 1)[0]))
    except ValueError:
        return "not an address"
    candidates = [address]
    if isinstance(address, ipaddress.IPv6Address):
        embedded = address.sixtofour
        if address in _NAT64:
            embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        if embedded is not None:
            if public_only:
                return "a translated address"
            candidates.append(embedded)
    for candidate in candidates:
        if candidate in CLOUD_METADATA_IPS:
            return "a cloud metadata address"
        if candidate.is_link_local or candidate.is_multicast or candidate.is_unspecified:
            return "a link-local, multicast or unspecified address"
        if candidate == _BROADCAST:
            return "the broadcast address"
        if public_only and not candidate.is_global:
            return "not a public address"
    return None


async def resolve_host(host: str, port: int) -> list[str]:
    """Every address the system resolver gives for ``host`` (off the loop: the default executor)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


def _parse(url: str, label: str):
    """The single parse: ``(parsed, scheme, normalised host, port)`` or ``ValueError``."""
    if not isinstance(url, str) or not 0 < len(url) <= 4096 or url != url.strip() or _has_unsafe_text(url):
        raise ValueError(f"{label} is not a valid address")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} is not a valid address") from exc
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"{label} must use http or https")
    if "@" in parsed.netloc:
        raise ValueError(f"{label} must not contain credentials")
    if " " in parsed.netloc or parsed.netloc.endswith(":"):
        raise ValueError(f"{label} is not a valid address")
    host = normalise_host(parsed.hostname, label)
    # The shared LAN-service rules (metadata names, numeric forms), asked of the normalised host.
    assert_safe_lan_service_url(f"{scheme}://{f'[{host}]' if ':' in host else host}/", label=label)
    if _is_ip(host):
        refusal = address_refusal(host, public_only=False)
        if refusal:
            raise ValueError(f"{label} must not point to {refusal}")
    return parsed, scheme, host, port or (443 if scheme == "https" else 80)


def _same_host(host: str | None) -> str | None:
    try:
        return normalise_host(host, "host")
    except ValueError:
        return None


def _tls_context() -> ssl.SSLContext:
    """What the rest of the app trusts: ``SSL_CERT_FILE`` / ``SSL_CERT_DIR``, else certifi."""
    if os.environ.get("SSL_CERT_FILE"):
        return ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
    if os.environ.get("SSL_CERT_DIR"):
        return ssl.create_default_context(capath=os.environ["SSL_CERT_DIR"])
    return ssl.create_default_context(cafile=certifi.where())


#: (normalised host, port, checked address) of the request being sent in this task.
_PIN: ContextVar[tuple[str, int, str] | None] = ContextVar("model_provider_outbound_pin", default=None)


class _CheckedStream(httpcore.AsyncNetworkStream):
    """A connection OUR socket layer opened; it remembers the name and port it was checked for."""

    def __init__(self, inner: httpcore.AsyncNetworkStream, checked: tuple[str, int]):
        self._inner = inner
        self.checked = checked

    async def read(self, max_bytes, timeout=None):
        return await self._inner.read(max_bytes, timeout)

    async def write(self, buffer, timeout=None):
        await self._inner.write(buffer, timeout)

    async def aclose(self):
        await self._inner.aclose()

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return _CheckedStream(await self._inner.start_tls(ssl_context, server_hostname, timeout), self.checked)

    def get_extra_info(self, info):
        return self._inner.get_extra_info(info)


class _PinnedSockets(httpcore.AsyncNetworkBackend):
    """The socket layer: a connection for a host is opened to the address ``_send`` checked for it.

    It never resolves a name. A host without a pin — nobody resolved and checked it for this
    request — gets no socket at all.
    """

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None):
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        pin = _PIN.get()
        if pin is None or (_same_host(host), port) != pin[:2]:
            raise httpcore.ConnectError("No checked address for this host")
        stream = await self._inner.connect_tcp(
            pin[2], port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )
        return _CheckedStream(stream, pin[:2])

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("Unix sockets are not a model source")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _from_core(exc: Exception) -> httpx.HTTPError:
    """httpcore's failure under httpx's name for it (the two libraries name them alike)."""
    kind = getattr(httpx, type(exc).__name__, None)
    if not (isinstance(kind, type) and issubclass(kind, httpx.TransportError)):
        kind = httpx.TimeoutException if isinstance(exc, httpcore.TimeoutException) else httpx.TransportError
    return kind(type(exc).__name__)


_CORE_ERRORS = (
    httpcore.TimeoutException,
    httpcore.NetworkError,
    httpcore.ProtocolError,
    httpcore.ProxyError,
    httpcore.UnsupportedProtocol,
    httpcore.ConnectionNotAvailable,
)


class _Body(httpx.AsyncByteStream):
    def __init__(self, stream):
        self._stream = stream

    async def __aiter__(self):
        try:
            async for part in self._stream:
                yield part
        except _CORE_ERRORS as exc:
            raise _from_core(exc) from exc

    async def aclose(self) -> None:
        await self._stream.aclose()


class _PinnedHTTP(httpx.AsyncBaseTransport):
    """The HTTP layer: a connection pool WE own, over the pinning socket layer.

    Built from httpcore's public constructor (``network_backend=``) — no attribute of httpx's own
    transport is reached into, so a client-library release cannot quietly put its own sockets
    back. Two runtime properties, neither left to a test:

    * a request without a pin is refused before the pool is asked;
    * an answer is accepted only when it arrived on a connection our socket layer opened for this
      request's name and port (``_CheckedStream``) — anything else is closed unread.
    """

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None, *, ssl_context: ssl.SSLContext | None = None):
        self.sockets = _PinnedSockets(inner)
        self.ssl_context = ssl_context
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=20,
            max_keepalive_connections=10,
            keepalive_expiry=5.0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=self.sockets,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        pin = _PIN.get()
        host = _same_host(request.url.host)
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        if pin is None or (host, port) != pin[:2]:
            raise httpx.ConnectError("No checked address for this request")
        core = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        try:
            answer = await self._pool.handle_async_request(core)
        except _CORE_ERRORS as exc:
            raise _from_core(exc) from exc
        stream = answer.extensions.get("network_stream")
        if not isinstance(stream, _CheckedStream) or stream.checked != pin[:2]:
            await answer.aclose()
            raise OutboundRefused("The answer did not come over a checked connection", "transport_disabled")
        return httpx.Response(
            status_code=answer.status,
            headers=answer.headers,
            stream=_Body(answer.stream),
            extensions=answer.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


def _pinned_http(inner: httpcore.AsyncNetworkBackend | None = None) -> _PinnedHTTP:
    """The pinning HTTP layer with certificate verification ON (``_tls_context``: CERT_REQUIRED,
    host name checked), done for the request's host name."""
    return _PinnedHTTP(inner, ssl_context=_tls_context())


# ---- the proof that the pin holds in THIS process --------------------------------------------

#: A name nothing resolves (RFC 6761) pinned to an address nothing routes (RFC 5737).
PROBE_HOST = "pin-check.invalid"
PROBE_ADDRESS = "192.0.2.1"
#: None until the first use proved (True) or disproved (False) the socket layer.
_pinning_proven: bool | None = None


class _ProbeStream(httpcore.AsyncNetworkStream):
    def __init__(self):
        self._answer = b""

    async def read(self, max_bytes, timeout=None):
        data, self._answer = self._answer[:max_bytes], self._answer[max_bytes:]
        return data

    async def write(self, buffer, timeout=None):
        if b"\r\n\r\n" in bytes(buffer):
            self._answer += b"HTTP/1.1 204 No Content\r\n\r\n"

    async def aclose(self):
        pass

    def get_extra_info(self, info):
        return None


class _ProbeSockets(httpcore.AsyncNetworkBackend):
    """Sockets that connect nowhere: they record what they were asked for."""

    def __init__(self):
        self.asked: list[tuple[str, int]] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.asked.append((host, port))
        return _ProbeStream()

    async def sleep(self, seconds: float) -> None:
        return None


async def _prove_pinning() -> bool:
    """One request to a sentinel name pinned to a sentinel address, over stub sockets.

    True only when it reached OUR socket layer, at the pinned address, and nothing else — and a
    request without a pin reached nothing. Any surprise (an exception included) is False.
    """
    probe = _ProbeSockets()
    try:
        async with httpx.AsyncClient(
            transport=_PinnedHTTP(probe), trust_env=False, follow_redirects=False, timeout=5.0
        ) as client:
            token = _PIN.set((PROBE_HOST, 80, PROBE_ADDRESS))
            try:
                answer = await client.send(httpx.Request("GET", f"http://{PROBE_HOST}/"))
            finally:
                _PIN.reset(token)
            if answer.status_code != 204 or probe.asked != [(PROBE_ADDRESS, 80)]:
                return False
            try:
                await client.send(httpx.Request("GET", f"http://unpinned.{PROBE_HOST}/"))
            except (httpx.HTTPError, OutboundRefused):
                return probe.asked == [(PROBE_ADDRESS, 80)]
            return False
    except Exception:
        return False


def transport_disabled() -> bool:
    """True once this process found that it cannot pin connections. Stays so until a restart."""
    return _pinning_proven is False


async def _require_pinning() -> None:
    global _pinning_proven
    if _pinning_proven is None:
        _pinning_proven = await _prove_pinning()
        if not _pinning_proven:
            logger.error(
                "Model sources with a configured address are switched off: the installed HTTP library "
                "does not connect through BamDude's checked-address socket layer"
            )
    if not _pinning_proven:
        raise OutboundRefused(
            "Model sources are switched off: this installation cannot pin its connections to checked addresses",
            "transport_disabled",
        )


@dataclass(frozen=True)
class PinnedOrigin:
    """An administrator-configured base address: the origin triple and its path prefix."""

    scheme: str
    host: str
    port: int
    prefix: str
    base: str

    @classmethod
    def admit(cls, url: str, *, label: str) -> PinnedOrigin:
        """Raise ``ValueError`` (a sentence for the form) unless ``url`` may be a configured origin."""
        parsed, scheme, host, port = _parse(url, label)
        if parsed.query or parsed.fragment or "?" in url or "#" in url:
            raise ValueError(f"{label} must not contain a query or a fragment")
        prefix = parsed.path.rstrip("/")
        if any(segment in ("", ".", "..") for segment in unquote(prefix).split("/")[1:]):
            raise ValueError(f"{label} has an invalid path")
        return cls(scheme=scheme, host=host, port=port, prefix=prefix, base=url.rstrip("/"))

    def owns(self, url: httpx.URL) -> bool:
        port = url.port or (443 if url.scheme == "https" else 80)
        return (url.scheme, _same_host(url.host), port) == (self.scheme, self.host, self.port)


@dataclass
class GuardedAnswer:
    """One answer. ``redirect`` names a redirect that was NOT followed:
    ``same_origin`` / ``scheme_upgrade`` (http → https on the same host) / ``foreign``.
    ``foreign`` is True when the bytes come from the storage hop, which never saw the token."""

    status: int
    content_type: str
    foreign: bool = False
    redirect: str | None = None
    content_length: int | None = None
    body: bytes = b""
    _response: Any = field(default=None, repr=False)

    async def chunks(self, cap: int) -> AsyncIterator[bytes]:
        """The body, refused past ``cap`` bytes (before reading when the length is declared)."""
        if self.content_length is not None and self.content_length > cap:
            raise OutboundTooLarge(cap)
        size = 0
        try:
            # The bytes of the wire, never inflated: ``stream`` refused any content encoding.
            # (A scripted client in tests hands over a response it has already read whole.)
            scripted = self._response.is_stream_consumed
            async for chunk in _one(self._response.content) if scripted else self._response.aiter_raw():
                size += len(chunk)
                if size > cap:
                    raise OutboundTooLarge(cap)
                yield chunk
        except httpx.HTTPError as exc:
            raise OutboundRefused(f"The connection failed while reading: {type(exc).__name__}", "unreachable") from exc

    def json(self) -> Any:
        """The parsed body, or None when it is not JSON (a plain-text 401 under a JSON content type)."""
        try:
            return json.loads(self.body)
        except ValueError:
            return None


async def _one(data: bytes) -> AsyncIterator[bytes]:
    yield data


class GuardedTransport:
    """The only HTTP door of a configured-origin provider."""

    def __init__(self, origin: PinnedOrigin, *, client: httpx.AsyncClient | None = None, sockets=None):
        self.origin = origin
        self._owns_client = client is None
        # ``client`` and ``sockets`` are TEST seams (a scripted client; stub sockets under the
        # pinning layer). Application code passes neither — a guard test scans for it.
        self._client = client
        self._sockets = sockets

    async def _http(self) -> httpx.AsyncClient:
        """The client, built at first use — only ever over the pinning HTTP layer."""
        if self._client is None:
            layer = _pinned_http(self._sockets)
            if not isinstance(layer, _PinnedHTTP):
                # Never an ordinary client instead: it would resolve names itself.
                raise OutboundRefused("The checked-address transport could not be built", "transport_disabled")
            # ``trust_env=False``: a proxy named by the environment would resolve the name itself,
            # past the address check below. The trust store still follows SSL_CERT_FILE / SSL_CERT_DIR.
            self._client = httpx.AsyncClient(
                timeout=DEFAULT_TIMEOUT, follow_redirects=False, trust_env=False, transport=layer
            )
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    # ---- building ------------------------------------------------------

    def _url(self, path: str) -> str:
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or path.startswith("//")
            or "?" in path
            or "#" in path
            or _has_unsafe_text(path)
            or any(segment in (".", "..") for segment in unquote(path).split("/"))
        ):
            raise OutboundRefused("Refusing a request path that could leave the configured address", "bad_path")
        return f"{self.origin.base}{path}"

    async def _send(self, method: str, url: httpx.URL, *, headers=None, data=None, public_only=False):
        """Resolve, check EVERY address, connect to the checked one.

        The name is resolved here, once, and the socket is opened to an address that passed
        (``_PinnedSockets``) — nothing resolves it again, so an answer that changes between check
        and connect (DNS rebinding) cannot move the request. The request URL keeps the NAME: the
        connection pool, ``Host``, the TLS server name and certificate verification are all by
        name, so two names on one address never share a connection.
        """
        await _require_pinning()
        client = await self._http()
        try:
            host = normalise_host(url.host, "The address")
        except ValueError as exc:
            raise OutboundRefused(str(exc), "address_refused") from exc
        port = url.port or (443 if url.scheme == "https" else 80)
        if _is_ip(host):
            addresses = [host]
        else:
            try:
                addresses = await resolve_host(host, port)
            except OSError as exc:
                raise OutboundRefused(f"Could not resolve the host name: {type(exc).__name__}", "unreachable") from exc
            if not addresses:
                raise OutboundRefused("The host name has no address", "unreachable")
        for address in addresses:
            refusal = address_refusal(address, public_only=public_only)
            if refusal:
                raise OutboundRefused(f"Refusing to connect: the host resolves to {refusal}", "address_refused")
        # Built directly, never through ``client.build_request``: no client default and no cookie
        # an earlier answer set is merged in.
        sent = {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity", **(headers or {})}
        address = addresses[0].split("%", 1)[0]
        request = httpx.Request(method, url, headers=sent, data=data, extensions={"pinned_address": address})
        # The pin travels with THIS call (a context variable, read by the socket layer in the same
        # task): two requests never see each other's address, whatever policy checked it.
        token = _PIN.set((host, port, address))
        try:
            return await client.send(request, stream=True, follow_redirects=False)
        except httpx.HTTPError as exc:
            # The type only: an httpx message may carry the URL, and a URL may carry a signature.
            raise OutboundRefused(f"Could not connect: {type(exc).__name__}", "unreachable") from exc
        finally:
            _PIN.reset(token)

    def _classify(self, current: httpx.URL, location: str | None) -> tuple[str, str | None]:
        """``(kind, absolute target)`` of a redirect; raises for a Location nobody may follow."""
        if not location or location != location.strip() or _has_unsafe_text(location):
            raise OutboundRefused("The server sent a redirect without a usable address", "bad_redirect")
        target = urljoin(str(current), location)
        try:
            parsed = httpx.URL(target)
        except (httpx.InvalidURL, ValueError) as exc:
            raise OutboundRefused("The server sent a redirect without a usable address", "bad_redirect") from exc
        if self.origin.owns(parsed):
            return "same_origin", target
        same_host = _same_host(parsed.host) == self.origin.host
        if same_host and self.origin.scheme == "http" and parsed.scheme == "https":
            return "scheme_upgrade", target
        return "foreign", target

    def _admit_foreign(self, target: str) -> httpx.URL:
        """The storage hop's address: https, no credentials, a host that may be public.

        Whether it IS public is decided on the resolved addresses, in ``_send(public_only=True)``.
        """
        try:
            _parsed, scheme, host, _port = _parse(target, "The storage address")
        except ValueError as exc:
            raise OutboundRefused(f"Refusing the redirect to file storage: {exc}", "foreign_refused") from exc
        if scheme != "https":
            raise OutboundRefused("Refusing a redirect to file storage over plain http", "foreign_refused")
        if _is_ip(host) and address_refusal(host, public_only=True):
            raise OutboundRefused("Refusing a redirect to an address that is not public", "foreign_refused")
        url = httpx.URL(target)
        # A presigned address is signed over its exact bytes: send it only when the client keeps them.
        wire = f"{url.scheme}://{url.netloc.decode('ascii')}{url.raw_path.decode('ascii')}"
        if wire.lower() != target.split("#", 1)[0].lower() or url.raw_path.decode("ascii") not in target:
            raise OutboundRefused("The storage address cannot be sent unchanged", "foreign_refused")
        return url

    # ---- requests ------------------------------------------------------

    @asynccontextmanager
    async def stream(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        accept: str | None = None,
        bearer: str | None = None,
        foreign_hop: bool = False,
        method: str = "GET",
        data: Mapping[str, str] | None = None,
    ) -> AsyncIterator[GuardedAnswer]:
        """One request to the pinned origin; the body is read by the caller through ``chunks``."""
        headers = {"Accept": accept} if accept else {}
        if bearer:
            # Only ever on this request: it goes to the pinned origin. The storage hop builds its own.
            headers["Authorization"] = f"Bearer {bearer}"
        url = httpx.URL(self._url(path), params=dict(params) if params else None)
        response = await self._send(method, url, headers=headers, data=data)
        foreign = False
        redirect: str | None = None
        try:
            if response.status_code in REDIRECT_STATUSES:
                redirect, target = self._classify(url, response.headers.get("Location"))
                if redirect == "foreign" and foreign_hop and method == "GET":
                    await response.aclose()
                    response = await self._send("GET", self._admit_foreign(target), public_only=True)
                    foreign, redirect = True, None
                    if response.status_code in REDIRECT_STATUSES:
                        raise OutboundRefused("File storage redirected again", "redirect_chain")
            encoding = response.headers.get("Content-Encoding", "").strip().lower()
            if redirect is None and encoding not in ("", "identity"):
                # Asked for identity. Inflating would put the byte caps behind the decompressor
                # (a 300 KB body is 300 MiB of zeros); the answer is refused unread instead.
                raise OutboundRefused("The server compressed its answer although asked not to", "encoded")
            length = response.headers.get("Content-Length")
            yield GuardedAnswer(
                status=response.status_code,
                content_type=response.headers.get("Content-Type", ""),
                foreign=foreign,
                redirect=redirect,
                content_length=int(length) if length and length.isdigit() else None,
                _response=response,
            )
        finally:
            # Shielded: a cancelled caller must still give the connection back.
            await asyncio.shield(response.aclose())

    async def _whole(self, path: str, cap: int, **kwargs) -> GuardedAnswer:
        async with self.stream(path, **kwargs) as answer:
            if answer.redirect is None:
                answer.body = b"".join([chunk async for chunk in answer.chunks(cap)])
            answer._response = None
            return answer

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        accept: str | None = None,
        bearer: str | None = None,
        cap: int = _JSON_CAP,
    ) -> GuardedAnswer:
        """A small answer read whole. No redirect is followed."""
        return await self._whole(path, cap, params=params, accept=accept, bearer=bearer)

    async def post_form(self, path: str, data: Mapping[str, str], *, accept: str = "application/json") -> GuardedAnswer:
        """A form POST to the origin (the token endpoint). Never redirected: a POST would lose its body."""
        return await self._whole(path, _JSON_CAP, accept=accept, method="POST", data=data)
