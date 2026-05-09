"""Tests for ``TRUSTED_FRAME_ORIGINS`` parsing + ``security_headers_middleware``
behaviour (#1191).

Default behaviour is strict: ``X-Frame-Options: SAMEORIGIN`` plus
``frame-ancestors 'none'``. Operators can opt into iframe embedding from
trusted origins (e.g. Home Assistant on a different port) via the
``TRUSTED_FRAME_ORIGINS`` env var; when set, ``X-Frame-Options`` is dropped
and ``frame-ancestors`` includes the allowlist with ``'self'`` always present.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from starlette.responses import Response


def _parse_origins(value: str | None) -> tuple[str, ...]:
    """Re-import the parser with a specific env var, return its result.

    The function reads ``os.environ`` on every call, so we can flip the var
    in-place rather than reloading the module.
    """
    from backend.app import main as main_module

    old = os.environ.get("TRUSTED_FRAME_ORIGINS")
    try:
        if value is None:
            os.environ.pop("TRUSTED_FRAME_ORIGINS", None)
        else:
            os.environ["TRUSTED_FRAME_ORIGINS"] = value
        return main_module._parse_trusted_frame_origins()
    finally:
        if old is None:
            os.environ.pop("TRUSTED_FRAME_ORIGINS", None)
        else:
            os.environ["TRUSTED_FRAME_ORIGINS"] = old


class TestParseTrustedFrameOrigins:
    def test_empty_env_returns_empty_tuple(self):
        assert _parse_origins("") == ()

    def test_unset_env_returns_empty_tuple(self):
        assert _parse_origins(None) == ()

    def test_single_origin(self):
        assert _parse_origins("http://homeassistant.local:8123") == ("http://homeassistant.local:8123",)

    def test_multiple_origins(self):
        result = _parse_origins("http://homeassistant.local:8123,https://ha.example.com")
        assert result == ("http://homeassistant.local:8123", "https://ha.example.com")

    def test_whitespace_around_entries_stripped(self):
        result = _parse_origins("  http://a.local:1 ,   https://b.local:2  ")
        assert result == ("http://a.local:1", "https://b.local:2")

    def test_empty_segment_skipped(self):
        result = _parse_origins("http://a.local,,https://b.local")
        assert result == ("http://a.local", "https://b.local")

    def test_non_http_scheme_dropped(self):
        # ftp://, javascript:, file:// — never a valid frame ancestor.
        assert _parse_origins("ftp://attacker.example,http://ok.local") == ("http://ok.local",)
        assert _parse_origins("javascript:alert(1)") == ()

    def test_missing_host_dropped(self):
        assert _parse_origins("http://") == ()

    def test_path_dropped(self):
        # frame-ancestors only takes scheme://host[:port], no path.
        assert _parse_origins("http://ha.local/dashboard") == ()

    def test_query_or_fragment_dropped(self):
        assert _parse_origins("http://ha.local?foo=1") == ()
        assert _parse_origins("http://ha.local#frag") == ()

    def test_wildcard_in_host_dropped(self):
        # Wildcards would defeat the allowlist purpose; reject explicitly.
        assert _parse_origins("http://*.example.com") == ()

    def test_root_path_kept(self):
        # Trailing slash is degenerate-but-harmless; treat as bare host.
        assert _parse_origins("http://ha.local:8123/") == ("http://ha.local:8123",)


# ─── middleware behaviour ──────────────────────────────────────────────────


def _make_request(path: str = "/api/v1/auth/status", scheme: str = "http"):
    """Minimal Request-shaped stub — security_headers_middleware reads only
    ``request.url.path`` + ``request.url.scheme``."""
    return SimpleNamespace(url=SimpleNamespace(path=path, scheme=scheme))


@pytest.mark.asyncio
async def test_default_strict_mode_emits_xframe_and_frame_ancestors_none(monkeypatch):
    from backend.app import main as main_module

    monkeypatch.setattr(main_module, "_TRUSTED_FRAME_ORIGINS", ())

    async def call_next(_request):
        return Response("ok")

    response = await main_module.security_headers_middleware(_make_request(), call_next)

    assert response.headers.get("X-Frame-Options") == "SAMEORIGIN"
    csp = response.headers.get("Content-Security-Policy", "")
    assert "frame-ancestors 'none';" in csp


@pytest.mark.asyncio
async def test_trusted_origins_drops_xframe_and_relaxes_csp(monkeypatch):
    from backend.app import main as main_module

    monkeypatch.setattr(main_module, "_TRUSTED_FRAME_ORIGINS", ("http://homeassistant.local:8123",))

    async def call_next(_request):
        return Response("ok")

    response = await main_module.security_headers_middleware(_make_request(), call_next)

    assert "X-Frame-Options" not in response.headers
    csp = response.headers.get("Content-Security-Policy", "")
    assert "frame-ancestors 'self' http://homeassistant.local:8123;" in csp
    # Strict 'none' must not leak through the catch-all branch.
    fa_segment = csp.split("frame-ancestors", 1)[1].split(";", 1)[0]
    assert "'none'" not in fa_segment


@pytest.mark.asyncio
async def test_trusted_origins_applies_to_docs_branch(monkeypatch):
    from backend.app import main as main_module

    monkeypatch.setattr(main_module, "_TRUSTED_FRAME_ORIGINS", ("https://ha.example.com",))

    async def call_next(_request):
        return Response("ok")

    response = await main_module.security_headers_middleware(_make_request(path="/docs"), call_next)

    csp = response.headers.get("Content-Security-Policy", "")
    assert "frame-ancestors 'self' https://ha.example.com;" in csp


@pytest.mark.asyncio
async def test_other_security_headers_unchanged(monkeypatch):
    from backend.app import main as main_module

    async def call_next(_request):
        return Response("ok")

    for origins in [(), ("http://homeassistant.local:8123",)]:
        monkeypatch.setattr(main_module, "_TRUSTED_FRAME_ORIGINS", origins)
        response = await main_module.security_headers_middleware(_make_request(), call_next)
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
