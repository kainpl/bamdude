"""Unit tests for 2FA helper functions in mfa.py."""

import base64
import string

import pytest
from passlib.context import CryptContext

from backend.app.api.routes.mfa import _generate_backup_codes, _generate_totp_qr_b64


class TestBackupCodeGeneration:
    """Tests for backup code helpers."""

    def test_generates_ten_codes(self):
        plain, hashed = _generate_backup_codes()
        assert len(plain) == 10
        assert len(hashed) == 10

    def test_codes_are_eight_chars(self):
        plain, _ = _generate_backup_codes()
        for code in plain:
            assert len(code) == 8

    def test_codes_are_alphanumeric(self):
        allowed = set(string.ascii_uppercase + string.digits)
        plain, _ = _generate_backup_codes()
        for code in plain:
            assert all(c in allowed for c in code)

    def test_hashes_verify_against_plain(self):
        ctx = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
        plain, hashed = _generate_backup_codes()
        for p, h in zip(plain, hashed, strict=True):
            assert ctx.verify(p, h)

    def test_codes_are_unique(self):
        plain, _ = _generate_backup_codes()
        assert len(set(plain)) == 10


class TestTOTPQRCode:
    """Tests for QR code generation helper."""

    def test_generates_base64_png(self):
        uri = "otpauth://totp/BamDude:testuser?secret=BASE32SECRET&issuer=BamDude"
        result = _generate_totp_qr_b64(uri)
        decoded = base64.b64decode(result)
        assert decoded[:4] == b"\x89PNG"


class TestStandardEmailFallback:
    """`_resolve_standard_email_for_user_record` — #1569 standard-email fallback
    used only when email_claim is a non-email identity claim (upstream v0.2.4.5)."""

    @staticmethod
    def _provider(require_ev: bool):
        from types import SimpleNamespace

        return SimpleNamespace(id=1, require_email_verified=require_ev)

    def test_valid_verified_email_returned(self):
        from backend.app.api.routes.mfa import _resolve_standard_email_for_user_record

        p = self._provider(require_ev=True)
        claims = {"email": "Jdoe@Example.com", "email_verified": True}
        assert _resolve_standard_email_for_user_record(p, claims, "sub1") == "jdoe@example.com"

    def test_unverified_email_dropped_when_required(self):
        from backend.app.api.routes.mfa import _resolve_standard_email_for_user_record

        p = self._provider(require_ev=True)
        claims = {"email": "jdoe@example.com", "email_verified": False}
        assert _resolve_standard_email_for_user_record(p, claims, "sub1") is None

    def test_permissive_accepts_absent_verified(self):
        from backend.app.api.routes.mfa import _resolve_standard_email_for_user_record

        p = self._provider(require_ev=False)
        claims = {"email": "jdoe@example.com"}
        assert _resolve_standard_email_for_user_record(p, claims, "sub1") == "jdoe@example.com"

    def test_permissive_drops_explicit_false(self):
        from backend.app.api.routes.mfa import _resolve_standard_email_for_user_record

        p = self._provider(require_ev=False)
        claims = {"email": "jdoe@example.com", "email_verified": False}
        assert _resolve_standard_email_for_user_record(p, claims, "sub1") is None

    def test_malformed_email_dropped(self):
        from backend.app.api.routes.mfa import _resolve_standard_email_for_user_record

        p = self._provider(require_ev=False)
        assert _resolve_standard_email_for_user_record(p, {"email": "not-an-email"}, "sub1") is None

    def test_non_string_email_dropped(self):
        from backend.app.api.routes.mfa import _resolve_standard_email_for_user_record

        p = self._provider(require_ev=False)
        assert _resolve_standard_email_for_user_record(p, {"email": ["a@b.com"]}, "sub1") is None

    def test_missing_email_claim_returns_none(self):
        from backend.app.api.routes.mfa import _resolve_standard_email_for_user_record

        p = self._provider(require_ev=False)
        assert _resolve_standard_email_for_user_record(p, {"preferred_username": "jdoe"}, "sub1") is None
