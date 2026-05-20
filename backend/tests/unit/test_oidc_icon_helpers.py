"""Unit tests for the OIDC icon SSRF guard + content-type resolver.

Ported from upstream Bambuddy #1333 / commit 8a7598f6 with adaptation
to our import paths.
"""

from __future__ import annotations

import pytest

from backend.app.api.routes._oidc_helpers import assert_safe_public_https_url
from backend.app.services.oidc_icon import (
    OIDCIconUnavailableError,
    _resolve_content_type,
)


class TestAssertSafePublicHttpsUrl:
    """Stricter SSRF guard for OIDC icons — rejects loopback / private."""

    def test_https_public_hostname_accepted(self):
        assert_safe_public_https_url("https://cdn.example.com/icon.png")

    def test_http_rejected(self):
        with pytest.raises(ValueError, match="https"):
            assert_safe_public_https_url("http://cdn.example.com/icon.png")

    def test_file_scheme_rejected(self):
        with pytest.raises(ValueError, match="https"):
            assert_safe_public_https_url("file:///etc/passwd")

    def test_loopback_ipv4_rejected(self):
        with pytest.raises(ValueError, match="loopback"):
            assert_safe_public_https_url("https://127.0.0.1/icon.png")

    def test_loopback_ipv6_rejected(self):
        with pytest.raises(ValueError, match="loopback"):
            assert_safe_public_https_url("https://[::1]/icon.png")

    def test_ipv4_mapped_loopback_rejected(self):
        """``::ffff:127.0.0.1`` is unwrapped before the is_loopback check."""
        with pytest.raises(ValueError, match="loopback"):
            assert_safe_public_https_url("https://[::ffff:127.0.0.1]/icon.png")

    def test_private_rfc_1918_rejected(self):
        for host in ("10.0.0.1", "172.16.0.1", "192.168.1.1"):
            with pytest.raises(ValueError, match="private"):
                assert_safe_public_https_url(f"https://{host}/icon.png")

    def test_link_local_rejected(self):
        with pytest.raises(ValueError, match="link-local"):
            assert_safe_public_https_url("https://169.254.0.1/icon.png")

    def test_cloud_metadata_rejected(self):
        for host in ("169.254.169.254", "100.100.100.200"):
            with pytest.raises(ValueError, match="metadata"):
                assert_safe_public_https_url(f"https://{host}/icon.png")

    def test_multicast_rejected(self):
        with pytest.raises(ValueError, match="multicast"):
            assert_safe_public_https_url("https://224.0.0.1/icon.png")

    def test_unspecified_rejected(self):
        with pytest.raises(ValueError, match="unspecified"):
            assert_safe_public_https_url("https://0.0.0.0/icon.png")

    def test_numeric_encoded_ip_rejected(self):
        """Decimal / hex IP forms libc accepts but ipaddress rejects."""
        for host in ("2130706433", "0x7f000001"):
            with pytest.raises(ValueError, match="numeric"):
                assert_safe_public_https_url(f"https://{host}/icon.png")


class TestResolveContentType:
    def test_whitelisted_mime_pass_through(self):
        for mime in ("image/png", "image/jpeg", "image/webp", "image/gif"):
            assert _resolve_content_type(mime, "/icon.png") == mime

    def test_application_octet_stream_with_png_extension_derives_png(self):
        assert _resolve_content_type("application/octet-stream", "/path/to/icon.PNG") == "image/png"

    def test_application_octet_stream_with_jpg_extension_derives_jpeg(self):
        assert _resolve_content_type("application/octet-stream", "/icon.jpg") == "image/jpeg"

    def test_application_octet_stream_without_image_extension_rejected(self):
        with pytest.raises(OIDCIconUnavailableError, match="octet-stream"):
            _resolve_content_type("application/octet-stream", "/icon.exe")

    def test_empty_content_type_rejected(self):
        with pytest.raises(OIDCIconUnavailableError, match="missing"):
            _resolve_content_type("", "/icon.png")

    def test_unsupported_content_type_rejected(self):
        with pytest.raises(OIDCIconUnavailableError, match="image/svg"):
            _resolve_content_type("image/svg+xml", "/icon.svg")

    def test_text_html_rejected(self):
        with pytest.raises(OIDCIconUnavailableError, match="text/html"):
            _resolve_content_type("text/html", "/icon.html")
