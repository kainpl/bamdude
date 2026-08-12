"""Bambu Cloud TOTP sign-in needs a CSRF handshake (upstream #2696).

Bambu added double-submit CSRF protection to the ``bambulab.com`` web origin —
which is where, and only where, the two-factor code is posted. A bare POST is
refused ``403 {"error": "CSRF error: missing_cookie"}`` **before the code is
evaluated**, so a perfectly good code was reported as invalid, permanently, for
every authenticator-app account.

Upstream verified the sequence against the live endpoint with a deliberately
invalid key: bare POST → ``missing_cookie``; ``GET /api/csrf`` mints a
``bbl_csrf_token`` cookie; cookie alone → ``missing_header``; cookie **plus**
``x-bbl-csrf-token`` header → reaches application logic. Landing on the sign-in
page first — the intuitive fix — does not work: it sets only Cloudflare's
``__cf_bm``.

``api.bambulab.com``, where every other call in this service goes including the
email-code 2FA path, is not gated. That is why only TOTP broke.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.bambu_cloud import BambuCloudService


def _response(status: int = 200, payload: dict | None = None, text: str | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = text if text is not None else "{}"
    resp.json.return_value = payload if payload is not None else {}
    resp.cookies = {}
    return resp


class TestTheHandshakeHappens:
    @pytest.mark.asyncio
    async def test_the_token_is_fetched_before_the_code_is_posted(self) -> None:
        svc = BambuCloudService()
        calls: list[str] = []

        svc._client = MagicMock()
        svc._client.get = AsyncMock(side_effect=lambda *a, **kw: (calls.append("get"), _response())[1])
        svc._client.post = AsyncMock(
            side_effect=lambda *a, **kw: (calls.append("post"), _response(200, {"accessToken": "tok"}))[1]
        )
        svc._client.cookies = {"bbl_csrf_token": "abc123"}

        result = await svc.verify_totp("key", "123456")

        assert calls == ["get", "post"], "the CSRF token must be minted before the POST, not after"
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_both_halves_are_sent(self) -> None:
        """The cookie rides along on httpx's shared jar; the header has to be
        echoed explicitly. Cookie alone yields ``missing_header``."""
        svc = BambuCloudService()
        svc._client = MagicMock()
        svc._client.get = AsyncMock(return_value=_response())
        svc._client.post = AsyncMock(return_value=_response(200, {"accessToken": "tok"}))
        svc._client.cookies = {"bbl_csrf_token": "abc123"}

        await svc.verify_totp("key", "123456")

        headers = svc._client.post.call_args.kwargs["headers"]
        assert headers["x-bbl-csrf-token"] == "abc123"

    @pytest.mark.asyncio
    async def test_the_csrf_endpoint_is_on_the_web_origin_not_the_api_host(self) -> None:
        svc = BambuCloudService()
        svc._client = MagicMock()
        svc._client.get = AsyncMock(return_value=_response())
        svc._client.post = AsyncMock(return_value=_response(200, {"accessToken": "tok"}))
        svc._client.cookies = {"bbl_csrf_token": "abc123"}

        await svc.verify_totp("key", "123456")

        assert svc._client.get.call_args.args[0] == "https://bambulab.com/api/csrf"

    @pytest.mark.asyncio
    async def test_the_china_region_uses_its_own_origin(self) -> None:
        svc = BambuCloudService(region="china")
        assert "bambulab.cn" in svc.base_url, "precondition: the china region points at the .cn API"

        svc._client = MagicMock()
        svc._client.get = AsyncMock(return_value=_response())
        svc._client.post = AsyncMock(return_value=_response(200, {"accessToken": "tok"}))
        svc._client.cookies = {"bbl_csrf_token": "abc123"}

        await svc.verify_totp("key", "123456")

        assert svc._client.get.call_args.args[0] == "https://bambulab.cn/api/csrf"
        assert svc._client.post.call_args.args[0] == "https://bambulab.cn/api/sign-in/tfa"


class TestFailuresAreNamedHonestly:
    @pytest.mark.asyncio
    async def test_no_token_means_we_do_not_post_at_all(self) -> None:
        """Posting without it would only earn a 403 that reads as a bad code."""
        svc = BambuCloudService()
        svc._client = MagicMock()
        svc._client.get = AsyncMock(return_value=_response())
        svc._client.post = AsyncMock(return_value=_response(200, {"accessToken": "tok"}))
        svc._client.cookies = {}  # endpoint minted nothing

        result = await svc.verify_totp("key", "123456")

        svc._client.post.assert_not_called()
        assert result["success"] is False
        assert "security token" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_a_network_failure_fetching_the_token_is_not_a_crash(self) -> None:
        svc = BambuCloudService()
        svc._client = MagicMock()
        svc._client.get = AsyncMock(side_effect=OSError("no route to host"))
        svc._client.post = AsyncMock()
        svc._client.cookies = {}

        result = await svc.verify_totp("key", "123456")

        assert result["success"] is False
        svc._client.post.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload",
        [
            {"error": "CSRF error: missing_cookie"},
            {"error": "CSRF error: missing_header"},
            {"reason": "missing_cookie"},
            {"reason": "missing_header"},
        ],
    )
    async def test_a_csrf_rejection_does_not_read_as_an_invalid_code(self, payload: dict) -> None:
        """This is the whole point of the item: the code was never evaluated, so
        saying "invalid code" sends the user chasing clock drift for a request
        Bambu had already refused."""
        svc = BambuCloudService()
        svc._client = MagicMock()
        svc._client.get = AsyncMock(return_value=_response())
        svc._client.post = AsyncMock(return_value=_response(403, payload, text=str(payload)))
        svc._client.cookies = {"bbl_csrf_token": "abc123"}

        result = await svc.verify_totp("key", "123456")

        assert result["success"] is False
        assert "invalid" not in result["message"].lower()
        assert "security-token" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_a_genuine_rejection_still_reports_its_own_message(self) -> None:
        svc = BambuCloudService()
        svc._client = MagicMock()
        svc._client.get = AsyncMock(return_value=_response())
        svc._client.post = AsyncMock(return_value=_response(400, {"message": "Verification code error"}))
        svc._client.cookies = {"bbl_csrf_token": "abc123"}

        result = await svc.verify_totp("key", "123456")

        assert result["success"] is False
        assert result["message"] == "Verification code error"


class TestHonestUserAgent:
    @pytest.mark.asyncio
    async def test_the_csrf_request_identifies_itself_as_bamdude(self) -> None:
        """The handshake adds a GET to the web origin, so it falls under the same
        no-impersonation policy as every other call this service makes."""
        svc = BambuCloudService()
        svc._client = MagicMock()
        svc._client.get = AsyncMock(return_value=_response())
        svc._client.post = AsyncMock(return_value=_response(200, {"accessToken": "tok"}))
        svc._client.cookies = {"bbl_csrf_token": "abc123"}

        with patch("backend.app.services.bambu_cloud._USER_AGENT", "BamDude/test (+url)"):
            await svc.verify_totp("key", "123456")

        assert svc._client.get.call_args.kwargs["headers"]["User-Agent"].startswith("BamDude/")
