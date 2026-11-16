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
* No redirect is followed, with ONE exception a caller asks for by name
  (``foreign_hop=True``, file bytes only): one hop to another origin — object
  storage behind the source — sent without the token, without cookies, to an
  address that passes the same host rules, https only unless the configured
  base itself is plain http, and that answer may not redirect again.
* Bodies are read through :meth:`GuardedAnswer.chunks` with a byte cap.

Provider code holds a :class:`GuardedTransport`, never an HTTP client of its
own (``tests/unit/test_model_providers_fetch_only_through_the_guard.py``).

Limits, stated: host names are not resolved here (as in every URL validator of
this code base), so a name that resolves to a refused address passes, and a
proxy named by the environment is honoured like for every other httpx call.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from backend.app.api.routes._url_safety import CLOUD_METADATA_IPS, assert_safe_lan_service_url, unwrap_ipv4_mapped
from backend.app.core.config import APP_VERSION
from backend.app.services.model_providers.base import ProviderUnavailableError

USER_AGENT = f"BamDude/{APP_VERSION} (+https://github.com/kainpl/bamdude)"

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
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


def _refuse_host(url: str, hostname: str, label: str) -> None:
    """The host rules shared by the configured address and the storage hop."""
    if not hostname or not hostname.isascii() or "%" in hostname or " " in hostname:
        raise ValueError(f"{label} must include a plain hostname")
    assert_safe_lan_service_url(url, label=label)
    try:
        address = unwrap_ipv4_mapped(ipaddress.ip_address(hostname))
    except ValueError:
        return
    embedded = None
    if isinstance(address, ipaddress.IPv6Address):
        embedded = address.sixtofour
        if address in _NAT64:
            embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    if embedded is not None and (embedded in CLOUD_METADATA_IPS or embedded.is_link_local):
        raise ValueError(f"{label} must not point to a cloud metadata endpoint")
    if address.is_link_local:
        raise ValueError(f"{label} must not point to a link-local address")


def _parse(url: str, label: str):
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
    hostname = (parsed.hostname or "").lower()
    _refuse_host(url, hostname, label)
    return parsed, scheme, hostname, port or (443 if scheme == "https" else 80)


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
        return (url.scheme, (url.host or "").lower(), port) == (self.scheme, self.host, self.port)


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
        self._client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=False)

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

    def _request(self, method: str, url: str, *, params=None, headers=None, bearer=None, data=None) -> httpx.Request:
        # Built directly, never through ``client.build_request``: no client default and no cookie
        # an earlier answer set is merged in.
        sent = {"User-Agent": USER_AGENT, "Accept": "*/*", **(headers or {})}
        if bearer:
            sent["Authorization"] = f"Bearer {bearer}"
        return httpx.Request(method, url, params=params, headers=sent, data=data)

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
        same_host = (parsed.host or "").lower() == self.origin.host
        if same_host and self.origin.scheme == "http" and parsed.scheme == "https":
            return "scheme_upgrade", target
        return "foreign", target

    def _admit_foreign(self, target: str) -> httpx.Request:
        try:
            _parsed, scheme, _host, _port = _parse(target, "The storage address")
        except ValueError as exc:
            raise OutboundRefused(f"Refusing the redirect to file storage: {exc}", "foreign_refused") from exc
        if scheme != "https" and self.origin.scheme != "http":
            raise OutboundRefused("Refusing a redirect from https to plain http", "foreign_refused")
        request = httpx.Request("GET", target, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        # A presigned address is signed over its exact bytes: send it only when the client keeps them.
        url = request.url
        wire = f"{url.scheme}://{url.netloc.decode('ascii')}{url.raw_path.decode('ascii')}"
        if wire != target.split("#", 1)[0]:
            raise OutboundRefused("The storage address cannot be sent unchanged", "foreign_refused")
        return request

    async def _send(self, request: httpx.Request) -> httpx.Response:
        try:
            return await self._client.send(request, stream=True, follow_redirects=False)
        except httpx.HTTPError as exc:
            # The type only: an httpx message may carry the URL, and a URL may carry a signature.
            raise OutboundRefused(f"Could not connect: {type(exc).__name__}", "unreachable") from exc

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
        headers = {"Accept": accept} if accept else None
        request = self._request(method, self._url(path), params=params, headers=headers, bearer=bearer, data=data)
        response = await self._send(request)
        foreign = False
        redirect: str | None = None
        try:
            if response.status_code in REDIRECT_STATUSES:
                redirect, target = self._classify(request.url, response.headers.get("Location"))
                if redirect == "foreign" and foreign_hop and method == "GET":
                    await response.aclose()
                    response = await self._send(self._admit_foreign(target))
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
