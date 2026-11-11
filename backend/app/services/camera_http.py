"""Camera URL spelling and owned HTTP hops; LAN and local restreams are allowed.

This is an initial spelling policy, not DNS/connect pinning. RTSP redirects
inside FFmpeg remain outside this HTTP opener's pre-connect guarantee.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
from contextlib import AsyncExitStack, asynccontextmanager
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp

from backend.app.api.routes._url_safety import CLOUD_METADATA_HOSTNAMES, CLOUD_METADATA_IPS, unwrap_ipv4_mapped

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_HTTP_REDIRECTS = 3
_NUMERIC_HOST = re.compile(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*", re.I)
_DNS_HOST = re.compile(r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)*\.?", re.I)
_NAT64_WELL_KNOWN_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")


def _blocked_camera_host(hostname: str) -> bool:
    if not hostname or not hostname.isascii() or "%" in hostname:
        return True
    host = hostname.lower().rstrip(".")
    if host in CLOUD_METADATA_HOSTNAMES or host == "metadata.google":
        return True
    try:
        address = unwrap_ipv4_mapped(ipaddress.ip_address(host))
    except ValueError:
        # libc accepts abbreviated/octal/hex IPv4 that ipaddress refuses.
        return bool(_NUMERIC_HOST.fullmatch(host)) or not bool(_DNS_HOST.fullmatch(hostname))
    embedded = None
    if isinstance(address, ipaddress.IPv6Address):
        embedded = address.sixtofour
        if address in _NAT64_WELL_KNOWN_PREFIX:
            embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return (
        address in CLOUD_METADATA_IPS
        or embedded in CLOUD_METADATA_IPS
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or address == ipaddress.IPv4Address("255.255.255.255")
        # Deprecated IPv4-compatible IPv6 spellings are not camera endpoints.
        or (isinstance(address, ipaddress.IPv6Address) and 1 < int(address) < 2**32)
    )


def _sanitize_camera_url(url: str, allowed_schemes=("http", "https", "rtsp", "rtsps")) -> str | None:
    if not isinstance(url, str) or not 0 < len(url) <= 4096:
        return None
    # urlsplit silently strips some controls; clients also disagree on '\\'.
    if any(
        ord(char) < 32 or 127 <= ord(char) <= 159 or (char.isspace() and char != " ") or char == "\\" for char in url
    ):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in allowed_schemes or not parsed.netloc:
            return None
        # urlsplit strips leading spaces. Only userinfo/path/query may carry
        # raw ASCII spaces; the scheme and host/port must remain unambiguous.
        if url[: len(parsed.scheme) + 3].lower() != f"{parsed.scheme.lower()}://":
            return None
        if _blocked_camera_host(parsed.hostname or ""):
            return None
        # Every transport resolves the host after the last @. Earlier raw @
        # characters belong to the saved username/password, not another host.
        authority = parsed.netloc.rsplit("@", 1)[-1]
        if " " in authority or authority.endswith(":") or (parsed.port is not None and not 1 <= parsed.port <= 65535):
            return None
        # Preserve opaque encoded credentials, path and query. Fragments never
        # belong to the server's request target, matching the previous contract.
        return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, parsed.query, ""))
    except (ValueError, UnicodeError):
        return None


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    return parsed.scheme, (parsed.hostname or "").lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


def _with_userinfo(url: str, userinfo: str | None) -> str:
    parsed = urlsplit(url)
    authority = parsed.netloc.rsplit("@", 1)[-1]
    if userinfo:
        authority = userinfo + "@" + authority
    return urlunsplit((parsed.scheme, authority, parsed.path, parsed.query, ""))


@asynccontextmanager
async def _validated_camera_get(session: aiohttp.ClientSession, url: str, *, timeout: float = 30.0):
    """Validate each hop before I/O, with one header deadline and sticky auth loss.

    The caller owns the final body deadline (or a streaming read-stall limit).
    The anonymous session shares the connector, never the source auth/cookies.
    """
    current = _sanitize_camera_url(url, ("http", "https"))
    if current is None:
        raise ValueError("camera URL was rejected")
    deadline = asyncio.get_running_loop().time() + timeout
    userinfo, separator, _ = urlsplit(current).netloc.rpartition("@")
    userinfo = userinfo if separator else None
    active = session
    anonymous = False
    visited = set()
    async with AsyncExitStack() as owned:
        for hop in range(_MAX_HTTP_REDIRECTS + 1):
            identity = _with_userinfo(current, None)
            if identity in visited:
                raise ValueError("camera redirect loop")
            visited.add(identity)
            request = active.get(current, allow_redirects=False)
            async with asyncio.timeout_at(deadline):
                response = await request.__aenter__()
            if response.status not in _REDIRECT_STATUSES:
                try:
                    yield response
                finally:
                    await request.__aexit__(None, None, None)
                return
            location = response.headers.get("Location")
            async with asyncio.timeout_at(deadline):
                await request.__aexit__(None, None, None)
            if hop == _MAX_HTTP_REDIRECTS:
                raise ValueError("camera redirect limit exceeded")
            # Check the raw header before urljoin can discard controls.
            if (
                not location
                or location.startswith(" ")
                or any(
                    ord(char) < 32 or 127 <= ord(char) <= 159 or char == "\\" or (char.isspace() and char != " ")
                    for char in location
                )
            ):
                raise ValueError("camera redirect location was rejected")
            target = _sanitize_camera_url(urljoin(current, location), ("http", "https"))
            if target is None or (current.startswith("https:") and target.startswith("http:")):
                raise ValueError("camera redirect target was rejected")
            if _origin(target) != _origin(current) and not anonymous:
                anonymous = True
                options = {
                    "timeout": getattr(session, "timeout", aiohttp.ClientTimeout(total=timeout)),
                    "cookie_jar": aiohttp.DummyCookieJar(),
                    "trust_env": False,
                    "auto_decompress": getattr(session, "_auto_decompress", True),
                    "headers": {"Accept-Encoding": "identity"},
                }
                if connector := getattr(session, "connector", None):
                    options.update(connector=connector, connector_owner=False)
                active = await owned.enter_async_context(aiohttp.ClientSession(**options))
            # Ignore peer-provided userinfo; keep only the originally supplied
            # credential while every hop remains on the original origin.
            current = _with_userinfo(target, None if anonymous else userinfo)
