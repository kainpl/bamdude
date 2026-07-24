"""Bambu Cloud token-expiry detection (upstream #2562 + its follow-up).

Two bugs, one credential:

1. ``set_token`` stamped ``token_expiry = now + 30 days`` every time a *stored*
   token was loaded, re-derived from *now* on every request. ``is_authenticated``
   could therefore never return False, so ``/cloud/status`` reported "connected"
   indefinitely while every cloud call 401'd.
2. The first fix then treated *any* 401 as expiry — but Bambu 401s for benign
   reasons (endpoint/region/scope refusals, Cloudflare edge, transient blips), so
   one stray rejection from a background poll signed the whole cloud integration
   out until a manual re-login.

The contract these tests pin: only Bambu's documented expiry body is a sign-out;
everything else is transient, and "unknown" must never present as "expired".
"""

import httpx
import pytest

from backend.app.services.bambu_cloud import (
    BambuCloudService,
    invalidate_validation_cache,
    is_expiry_401,
)


def _response(status: int, json_body=None, text: str | None = None) -> httpx.Response:
    request = httpx.Request("GET", "https://api.bambulab.com/v1/whatever")
    if json_body is not None:
        return httpx.Response(status, json=json_body, request=request)
    return httpx.Response(status, text=text or "", request=request)


@pytest.fixture(autouse=True)
def _clear_cache():
    invalidate_validation_cache()
    yield
    invalidate_validation_cache()


class TestIsExpiry401:
    def test_documented_expiry_body(self):
        assert is_expiry_401(_response(401, {"code": 4, "error": "Please login.", "message": ""})) is True

    def test_please_login_without_code(self):
        assert is_expiry_401(_response(401, {"error": "Please login."})) is True

    def test_plain_401_is_not_expiry(self):
        """A signature-less 401 is endpoint/edge noise, not a dead token."""
        assert is_expiry_401(_response(401, {"error": "forbidden for this region"})) is False

    def test_unparseable_body_is_not_expiry(self):
        assert is_expiry_401(_response(401, text="<html>Just a moment...</html>")) is False

    def test_non_dict_body_is_not_expiry(self):
        assert is_expiry_401(_response(401, ["nope"])) is False


class TestIsAuthenticated:
    def test_stored_token_no_longer_invents_an_expiry(self):
        """The #2562 root cause: loading a stored token must record no expiry."""
        svc = BambuCloudService()
        svc.set_token("stored-token")
        assert svc.token_expiry is None
        # Still "loaded" — is_authenticated only reports presence, by design.
        assert svc.is_authenticated is True

    def test_no_token_is_not_authenticated(self):
        assert BambuCloudService().is_authenticated is False


class TestNoteResponse:
    @pytest.mark.asyncio
    async def test_expiry_401_fires_the_callback_once(self):
        calls = []

        async def on_fail():
            calls.append(1)

        svc = BambuCloudService(on_auth_failure=on_fail)
        svc.set_token("dead-token")
        expiry = _response(401, {"code": 4, "error": "Please login."})

        assert await svc._note_response(expiry) is True
        assert await svc._note_response(expiry) is True  # reported at most once
        assert calls == [1]

    @pytest.mark.asyncio
    async def test_plain_401_does_not_fire_the_callback(self):
        calls = []

        async def on_fail():
            calls.append(1)

        svc = BambuCloudService(on_auth_failure=on_fail)
        svc.set_token("live-token")

        assert await svc._note_response(_response(401, {"error": "nope"})) is False
        assert calls == []

    @pytest.mark.asyncio
    async def test_non_401_is_ignored(self):
        svc = BambuCloudService(on_auth_failure=None)
        assert await svc._note_response(_response(200, {"ok": True})) is False

    @pytest.mark.asyncio
    async def test_callback_failure_is_swallowed(self):
        """Bookkeeping must never replace the 401 the caller needs to see."""

        async def boom():
            raise RuntimeError("db down")

        svc = BambuCloudService(on_auth_failure=boom)
        svc.set_token("dead-token")
        assert await svc._note_response(_response(401, {"code": 4})) is True


class TestValidateToken:
    def _service(self, handler, **kw) -> BambuCloudService:
        transport = httpx.MockTransport(handler)
        svc = BambuCloudService(client=httpx.AsyncClient(transport=transport), **kw)
        svc.set_token("tok")
        return svc

    @pytest.mark.asyncio
    async def test_no_token_is_false(self):
        assert await BambuCloudService().validate_token() is False

    @pytest.mark.asyncio
    async def test_200_accepts(self):
        svc = self._service(lambda request: httpx.Response(200, json={}))
        assert await svc.validate_token() is True

    @pytest.mark.asyncio
    async def test_signed_401_rejects(self):
        svc = self._service(lambda request: httpx.Response(401, json={"code": 4, "error": "Please login."}))
        assert await svc.validate_token() is False

    @pytest.mark.asyncio
    async def test_unsigned_401_is_unknown_not_rejected(self):
        """A stray 401 here must report unknown — never expire a live session."""
        svc = self._service(lambda request: httpx.Response(401, json={"error": "region"}))
        assert await svc.validate_token() is None

    @pytest.mark.asyncio
    async def test_5xx_is_unknown(self):
        svc = self._service(lambda request: httpx.Response(503, text="down"))
        assert await svc.validate_token() is None

    @pytest.mark.asyncio
    async def test_cloudflare_418_is_unknown(self):
        svc = self._service(lambda request: httpx.Response(418, text="Just a moment..."))
        assert await svc.validate_token() is None

    @pytest.mark.asyncio
    async def test_transport_error_is_unknown(self):
        def boom(request):
            raise httpx.ConnectError("no route", request=request)

        svc = self._service(boom)
        assert await svc.validate_token() is None

    @pytest.mark.asyncio
    async def test_verdict_is_cached(self):
        hits = []

        def handler(request):
            hits.append(1)
            return httpx.Response(200, json={})

        svc = self._service(handler)
        assert await svc.validate_token() is True
        assert await svc.validate_token() is True
        assert len(hits) == 1

    @pytest.mark.asyncio
    async def test_invalidate_clears_the_cached_verdict(self):
        hits = []

        def handler(request):
            hits.append(1)
            return httpx.Response(200, json={})

        svc = self._service(handler)
        await svc.validate_token()
        invalidate_validation_cache("tok")
        await svc.validate_token()
        assert len(hits) == 2
