"""The connection a user configures: one BamDude server, one API key, an optional CA and proxy headers (spec r2 §12).

Validated once, here, and frozen. Nothing in this module talks to the network.

- The server URL is ``http`` or ``https`` only, with no user info, query or fragment; its path is kept as the
  server's base path. Plain HTTP is allowed as an explicit choice (a LAN or VPN), never substituted for HTTPS.
- Proxy headers (a Cloudflare Access pair, for example) have valid names and values without CR/LF, are bounded in
  number and size, and may never override what the connector itself sends or what a proxy reserves.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

MAX_EXTRA_HEADERS = 8
MAX_HEADER_VALUE = 1024
_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}$")
# What the connector sends itself, what HTTP owns, and what a fronting proxy writes on its own.
RESERVED_HEADERS = frozenset(
    {
        "host",
        "authorization",
        "x-api-key",
        "x-bamdude-printer-identity",
        "content-length",
        "content-type",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "upgrade",
        "te",
        "trailer",
        "proxy-authorization",
        "cookie",
        "cf-connecting-ip",
        "cf-ipcountry",
        "cf-ray",
        "cf-visitor",
        "cf-worker",
        "cdn-loop",
        "true-client-ip",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-real-ip",
    }
)


class ConfigError(ValueError):
    """A configuration value is refused; ``code`` is stable, the text never contains the value itself."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def canonical_base_url(raw: str) -> str:
    """The server's base URL, canonical: lower-case scheme and host, no trailing slash, nothing after the path."""
    text = (raw or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise ConfigError("url_invalid") from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ConfigError("url_scheme")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ConfigError("url_userinfo")
    if parts.query or parts.fragment or text.endswith("?") or text.endswith("#"):
        raise ConfigError("url_query")
    try:
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ConfigError("url_invalid") from exc
    if not host:
        raise ConfigError("url_host")
    if any(ch.isspace() for ch in text):
        raise ConfigError("url_invalid")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


def validate_extra_headers(pairs) -> tuple[tuple[str, str], ...]:
    """Proxy headers as ``(name, value)`` pairs, each one checked; names are unique regardless of case."""
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, value in pairs or ():
        name = str(name).strip()
        value = str(value)
        if not _TOKEN.match(name):
            raise ConfigError("header_name")
        lowered = name.lower()
        if lowered in RESERVED_HEADERS or lowered in seen:
            raise ConfigError("header_reserved")
        if any(ch in value for ch in "\r\n\0") or len(value) > MAX_HEADER_VALUE:
            raise ConfigError("header_value")
        seen.add(lowered)
        result.append((name, value))
    if len(result) > MAX_EXTRA_HEADERS:
        raise ConfigError("header_count")
    return tuple(result)


def validate_api_key(raw: str) -> str:
    key = (raw or "").strip()
    if not key or any(ch.isspace() or ord(ch) < 0x21 or ord(ch) > 0x7E for ch in key) or len(key) > 256:
        raise ConfigError("api_key")
    return key


@dataclass(frozen=True)
class Connection:
    base_url: str
    api_key: str = field(repr=False)
    ca_file: str | None = None
    extra_headers: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    @property
    def uses_tls(self) -> bool:
        return self.base_url.startswith("https://")

    @classmethod
    def build(cls, base_url: str, api_key: str, ca_file: str | None = None, extra_headers=None) -> Connection:
        ca = (ca_file or "").strip() or None
        if ca is not None and not os.path.isfile(ca):
            raise ConfigError("ca_file")
        return cls(
            base_url=canonical_base_url(base_url),
            api_key=validate_api_key(api_key),
            ca_file=ca,
            extra_headers=validate_extra_headers(extra_headers),
        )
