"""Tests for Bambu Cloud service - TOTP and email verification flows."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.bambu_cloud import BambuCloudService


class TestBambuCloudLogin:
    """Test login flow detection (email vs TOTP)."""

    @pytest.fixture
    def cloud_service(self):
        """Create a BambuCloudService instance."""
        return BambuCloudService()

    @pytest.mark.asyncio
    async def test_login_detects_email_verification(self, cloud_service):
        """When loginType is verifyCode, should return email verification type."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "loginType": "verifyCode",
        }

        with patch.object(cloud_service._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            result = await cloud_service.login_request("test@example.com", "password")

            assert result["success"] is False
            assert result["needs_verification"] is True
            assert result["verification_type"] == "email"
            assert result["tfa_key"] is None
            assert "email" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_login_detects_totp(self, cloud_service):
        """When loginType is tfa, should return TOTP verification type with tfaKey."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "loginType": "tfa",
            "tfaKey": "test-tfa-key-123",
        }

        with patch.object(cloud_service._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            result = await cloud_service.login_request("test@example.com", "password")

            assert result["success"] is False
            assert result["needs_verification"] is True
            assert result["verification_type"] == "totp"
            assert result["tfa_key"] == "test-tfa-key-123"
            assert "authenticator" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_login_direct_success(self, cloud_service):
        """When accessToken is returned directly, should succeed without verification."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "accessToken": "test-access-token",
            "refreshToken": "test-refresh-token",
        }

        with patch.object(cloud_service._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            result = await cloud_service.login_request("test@example.com", "password")

            assert result["success"] is True
            assert result["needs_verification"] is False
            assert cloud_service.access_token == "test-access-token"

    @pytest.mark.asyncio
    async def test_login_failure(self, cloud_service):
        """When login fails, should return error message."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.json.return_value = {
            "message": "Invalid credentials",
        }

        with patch.object(cloud_service._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            result = await cloud_service.login_request("test@example.com", "wrong-password")

            assert result["success"] is False
            assert result["needs_verification"] is False
            assert "Invalid credentials" in result["message"]


class TestBambuCloudEmailVerification:
    """Test email verification flow."""

    @pytest.fixture
    def cloud_service(self):
        """Create a BambuCloudService instance."""
        return BambuCloudService()

    @pytest.mark.asyncio
    async def test_verify_code_success(self, cloud_service):
        """When email code is correct, should return success with token."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "accessToken": "test-access-token",
            "refreshToken": "test-refresh-token",
        }

        with patch.object(cloud_service._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            result = await cloud_service.verify_code("test@example.com", "123456")

            assert result["success"] is True
            assert cloud_service.access_token == "test-access-token"

    @pytest.mark.asyncio
    async def test_verify_code_failure(self, cloud_service):
        """When email code is incorrect, should return failure."""
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.json.return_value = {
            "message": "Invalid verification code",
        }

        with patch.object(cloud_service._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            result = await cloud_service.verify_code("test@example.com", "000000")

            assert result["success"] is False
            assert "Invalid" in result["message"] or "Verification failed" in result["message"]


class TestBambuCloudTOTPVerification:
    """Test TOTP verification flow."""

    @pytest.fixture(autouse=True)
    def _stub_csrf_handshake(self):
        """Keep the CSRF pre-flight off the network for every test in this class.

        ``verify_totp`` fetches a CSRF token from the ``bambulab.com`` web origin
        before posting the code (#2696), and returns early without posting when
        it cannot get one. The tests below patch only ``post``, so that GET would
        go out over the real network — succeeding on any machine that can reach
        bambulab.com, and failing on a CI runner, where the assertions would then
        be about a ``post`` that never happened.

        The handshake itself is covered end to end in
        ``test_cloud_totp_csrf.py``, including the no-token path, so stubbing it
        here removes a network dependency rather than any coverage.
        """
        with patch.object(BambuCloudService, "_fetch_csrf_token", new_callable=AsyncMock) as fetch:
            fetch.return_value = "csrf-token-for-tests"
            yield fetch

    @pytest.fixture
    def cloud_service(self):
        """Create a BambuCloudService instance."""
        return BambuCloudService()

    @pytest.mark.asyncio
    async def test_verify_totp_success(self, cloud_service):
        """When TOTP code is correct, should return success with token."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"token": "test-access-token"}'
        mock_response.json.return_value = {
            "token": "test-access-token",
        }
        mock_response.cookies = {}

        with patch.object(cloud_service._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            result = await cloud_service.verify_totp("test-tfa-key", "123456")

            assert result["success"] is True
            assert cloud_service.access_token == "test-access-token"

    @pytest.mark.asyncio
    async def test_verify_totp_uses_correct_endpoint(self, cloud_service):
        """TOTP verification should use bambulab.com, not api.bambulab.com."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"token": "test-token"}'
        mock_response.json.return_value = {"token": "test-token"}
        mock_response.cookies = {}

        with patch.object(cloud_service._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            await cloud_service.verify_totp("test-tfa-key", "123456")

            # Check the URL used
            call_args = mock_post.call_args
            url = call_args[0][0]
            assert "bambulab.com/api/sign-in/tfa" in url
            assert "api.bambulab.com" not in url

    @pytest.mark.asyncio
    async def test_verify_totp_empty_response(self, cloud_service):
        """When TOTP returns empty response, should handle gracefully."""
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = ""

        with patch.object(cloud_service._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            result = await cloud_service.verify_totp("test-tfa-key", "123456")

            assert result["success"] is False
            assert "empty response" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_verify_totp_cloudflare_blocked(self, cloud_service):
        """When Cloudflare blocks the request, the "Just a moment..." challenge
        body is detected and turned into an actionable message (#1575) rather
        than the opaque "Invalid response from Bambu Cloud"."""
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "<!DOCTYPE html><html><head><title>Just a moment...</title>"
        # json() raises an error when response is HTML
        mock_response.json.side_effect = ValueError("No JSON")

        with patch.object(cloud_service._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            result = await cloud_service.verify_totp("test-tfa-key", "123456")

            assert result["success"] is False
            assert "Cloudflare" in result["message"]
            assert "Invalid response" not in result["message"]

    @pytest.mark.asyncio
    async def test_verify_totp_uses_honest_bamdude_user_agent(self, cloud_service):
        """TOTP verification identifies as BamDude — no browser-impersonation
        UA, no spoofed Origin/Referer/Accept-Language. Bambu's 2026-05-12
        cloud-access statement requires honest client identification."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"token": "test-token"}'
        mock_response.json.return_value = {"token": "test-token"}
        mock_response.cookies = {}

        with patch.object(cloud_service._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            await cloud_service.verify_totp("test-tfa-key", "123456")

            call_args = mock_post.call_args
            headers = call_args[1]["headers"]
            assert headers["User-Agent"].startswith("BamDude/")
            assert "kainpl/bamdude" in headers["User-Agent"]
            # Browser-impersonation tokens must NOT leak in.
            assert "Mozilla" not in headers["User-Agent"]
            assert "Chrome" not in headers["User-Agent"]
            assert "Origin" not in headers
            assert "Referer" not in headers
            assert "Accept-Language" not in headers


# ===========================================================================
# Issue #1815: PFUS cloud user preset lookup silently 400s in resolver
# ===========================================================================


class TestSlicerSettingVersionParam:
    """`/v1/iot-service/api/slicer/setting` endpoints require ?version=XX.YY.ZZ.WW.

    The plural GET (`get_slicer_settings`) has always sent it. The singular
    GET (`get_setting_detail`) and DELETE (`delete_setting`) hit the same
    subtree and were silently omitting it since #1013's compliance rework
    (2026-05-12), which surfaced as #1815: every PFUS-prefix cloud user preset
    lookup in the slicer_filament_resolver 400'd, so BambuStudio saw the
    generic-material fallback instead of the user's actual custom profile
    (rescued in most cases by slot-tray_info_idx reuse or K-profile realign,
    Bgabor997's spool 54 had neither).
    """

    def _auth(self) -> BambuCloudService:
        cloud = BambuCloudService()
        cloud.access_token = "test-token"
        return cloud

    @pytest.mark.asyncio
    async def test_get_setting_detail_sends_version_param(self):
        """`get_setting_detail` must include the version query param — without
        it Bambu Cloud returns HTTP 400 'field version is not set'."""
        cloud = self._auth()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"filament_id": "P4d64437", "name": "Overture Matte PLA"}

        with patch.object(cloud._client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            result = await cloud.get_setting_detail("PFUS992454068158eb")

            assert result["filament_id"] == "P4d64437"
            url = mock_get.call_args[0][0]
            assert url.endswith("/v1/iot-service/api/slicer/setting/PFUS992454068158eb")
            params = mock_get.call_args.kwargs.get("params") or {}
            assert params.get("version"), "get_setting_detail must send ?version=… to avoid 400"

    @pytest.mark.asyncio
    async def test_get_setting_detail_error_includes_response_body(self):
        """The 400 body identifies the exact contract violation. Callers include
        it in log warnings so a next contract change is self-diagnostic instead
        of surfacing an opaque status code (which cost 50 days on #1815)."""
        cloud = self._auth()

        from backend.app.services.bambu_cloud import BambuCloudError

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "field 'version' is not set"

        with patch.object(cloud._client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            with pytest.raises(BambuCloudError) as exc:
                await cloud.get_setting_detail("PFUS992454068158eb")

            assert "400" in str(exc.value)
            assert "field 'version'" in str(exc.value)

    @pytest.mark.asyncio
    async def test_delete_setting_sends_version_param(self):
        """`delete_setting` hits the same subtree; same requirement applies."""
        cloud = self._auth()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"{}"
        mock_response.json.return_value = {}

        with patch.object(cloud._client, "delete", new_callable=AsyncMock) as mock_delete:
            mock_delete.return_value = mock_response

            result = await cloud.delete_setting("PFUS992454068158eb")

            assert result["success"] is True
            url = mock_delete.call_args[0][0]
            assert url.endswith("/v1/iot-service/api/slicer/setting/PFUS992454068158eb")
            params = mock_delete.call_args.kwargs.get("params") or {}
            assert params.get("version"), "delete_setting must send ?version=… to avoid 400"
