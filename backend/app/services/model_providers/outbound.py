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
  here, EVERY answer is tested (:func:`address_refusal`), and the request is
  sent to the tested address with the asked-for ``Host`` and TLS name — no
  second resolution between check and connect.
* No redirect is followed, with ONE exception a caller asks for by name
  (``foreign_hop=True``, file bytes only): one hop to another origin — object
  storage behind the source — sent without the token and without cookies,
  https only, to PUBLIC addresses only (loopback, private, link-local,
  carrier-grade NAT, unique-local and metadata addresses are refused wherever
  the configured origin lives), and that answer may not redirect again.
* Bodies are read through :meth:`GuardedAnswer.chunks` with a byte cap.

Provider code holds a :class:`GuardedTransport`, never an HTTP client of its
own (``tests/unit/test_model_providers_fetch_only_through_the_guard.py``).

A proxy named by the environment is NOT used: it would resolve names itself.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import ssl
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import certifi
import httpx

from backend.app.api.routes._url_safety import CLOUD_METADATA_IPS, assert_safe_lan_service_url, unwrap_ipv4_mapped
from backend.app.core.config import APP_VERSION
from backend.app.services.model_providers.base import ProviderUnavailableError

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
            async for chunk in self._response.aiter_bytes():
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


class GuardedTransport:
    """The only HTTP door of a configured-origin provider."""

    def __init__(self, origin: PinnedOrigin, *, client: httpx.AsyncClient | None = None):
        self.origin = origin
        self._owns_client = client is None
        # ``trust_env=False``: a proxy named by the environment would resolve the name itself, past
        # the address check below. The trust store still follows SSL_CERT_FILE / SSL_CERT_DIR.
        self._client = client or httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT, follow_redirects=False, trust_env=False, verify=_tls_context()
        )

    async def aclose(self) -> None:
        if self._owns_client:
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

        The name is resolved here, once, and the connection goes to an address that passed — the
        client never resolves it again, so an answer that changes between check and connect (DNS
        rebinding) cannot move the request. ``Host`` and the TLS name stay the host that was asked
        for, so virtual hosting and certificate verification are unchanged.
        """
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
        sent = {"User-Agent": USER_AGENT, "Accept": "*/*", **(headers or {}), "Host": url.netloc.decode("ascii")}
        extensions = {"sni_hostname": host} if url.scheme == "https" and not _is_ip(host) else {}
        pinned = url.copy_with(host=addresses[0].split("%", 1)[0])
        request = httpx.Request(method, pinned, headers=sent, data=data, extensions=extensions)
        try:
            return await self._client.send(request, stream=True, follow_redirects=False)
        except httpx.HTTPError as exc:
            # The type only: an httpx message may carry the URL, and a URL may carry a signature.
            raise OutboundRefused(f"Could not connect: {type(exc).__name__}", "unreachable") from exc

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
