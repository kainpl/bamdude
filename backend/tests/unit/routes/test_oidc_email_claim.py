"""Tests for OIDC ``email_claim`` + ``require_email_verified`` helpers (B.16, A.31).

Covers:
- ``_resolve_provider_email`` — Fall A (strict), Fall B (permissive), Fall C
  (custom claim) for Azure Entra ID's preferred_username / upn semantics.
- ``_enforce_auto_link_safety`` — blocks the unsafe combo regardless of which
  code path constructed the ORM object.
- Pydantic schema validators — auto_link + email + require_ev=False is
  rejected at schema time; custom-claim Azure configs are accepted.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend.app.api.routes.mfa import (
    _derive_oidc_username_seed,
    _enforce_auto_link_safety,
    _is_valid_email_shaped,
    _resolve_provider_email,
)
from backend.app.schemas.auth import (
    AUTO_LINK_REQUIREMENTS_ERROR,
    OIDCProviderCreate,
    OIDCProviderUpdate,
)


def _provider(**overrides) -> SimpleNamespace:
    """Lightweight stand-in for OIDCProvider — _resolve_provider_email only
    reads attributes, never persists, so a SimpleNamespace is enough."""
    base = {
        "id": 1,
        "name": "Test",
        "email_claim": "email",
        "require_email_verified": True,
        "auto_link_existing_accounts": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestEmailShape:
    @pytest.mark.parametrize(
        "value,ok",
        [
            ("user@example.com", True),
            ("a.b+c@sub.example.co.uk", True),
            ("@example.com", False),
            ("user@", False),
            ("user@nodot", False),
            ("", False),
            (None, False),
            ("a" * 250 + "@example.com", False),  # > 255 chars
        ],
    )
    def test_shape(self, value, ok):
        assert _is_valid_email_shaped(value) is ok


class TestResolveEmail:
    def test_fall_a_strict_verified_true_returns_email(self):
        provider = _provider()
        claims = {"email": "user@example.com", "email_verified": True}
        assert _resolve_provider_email(provider, claims, "sub-1") == "user@example.com"

    def test_fall_a_strict_verified_false_returns_none(self):
        provider = _provider()
        claims = {"email": "user@example.com", "email_verified": False}
        assert _resolve_provider_email(provider, claims, "sub-1") is None

    def test_fall_a_strict_verified_absent_returns_none(self):
        provider = _provider()
        claims = {"email": "user@example.com"}
        assert _resolve_provider_email(provider, claims, "sub-1") is None

    def test_fall_b_permissive_verified_absent_returns_email(self):
        provider = _provider(require_email_verified=False)
        claims = {"email": "user@example.com"}
        assert _resolve_provider_email(provider, claims, "sub-1") == "user@example.com"

    def test_fall_b_permissive_verified_false_drops_email(self):
        provider = _provider(require_email_verified=False)
        claims = {"email": "user@example.com", "email_verified": False}
        assert _resolve_provider_email(provider, claims, "sub-1") is None

    def test_fall_c_custom_claim_no_verified_gate(self):
        provider = _provider(email_claim="preferred_username", require_email_verified=True)
        claims = {"preferred_username": "user@example.com"}  # no email_verified
        assert _resolve_provider_email(provider, claims, "sub-1") == "user@example.com"

    def test_fall_c_custom_claim_uses_upn(self):
        provider = _provider(email_claim="upn", require_email_verified=True)
        claims = {"upn": "User@Tenant.OnMicrosoft.Com"}
        # Should be lowercase-stripped
        assert _resolve_provider_email(provider, claims, "sub-1") == "user@tenant.onmicrosoft.com"

    def test_fall_c_rejects_non_email_shaped_value(self):
        provider = _provider(email_claim="preferred_username")
        claims = {"preferred_username": "not-an-email"}
        assert _resolve_provider_email(provider, claims, "sub-1") is None

    def test_fall_a_rejects_non_email_shaped_value_even_when_verified(self):
        # Some providers mark numeric IDs as email_verified=True. SEC-2 shape
        # check must run before the verified gate.
        provider = _provider()
        claims = {"email": "12345", "email_verified": True}
        assert _resolve_provider_email(provider, claims, "sub-1") is None

    def test_non_string_claim_value_returns_none(self):
        provider = _provider(email_claim="preferred_username")
        claims = {"preferred_username": ["a@b.com", "c@d.com"]}  # list, not str
        assert _resolve_provider_email(provider, claims, "sub-1") is None


class TestEnforceAutoLinkSafety:
    def test_safe_default_no_auto_link(self):
        provider = _provider(auto_link_existing_accounts=False)
        _enforce_auto_link_safety(provider)  # no raise

    def test_safe_auto_link_with_strict_email(self):
        provider = _provider(auto_link_existing_accounts=True, require_email_verified=True)
        _enforce_auto_link_safety(provider)  # no raise

    def test_safe_auto_link_with_custom_claim(self):
        # Fall C — custom claim never gates on email_verified, so this is safe.
        provider = _provider(
            auto_link_existing_accounts=True,
            email_claim="preferred_username",
            require_email_verified=False,
        )
        _enforce_auto_link_safety(provider)  # no raise

    def test_unsafe_auto_link_with_email_and_no_verified(self):
        provider = _provider(
            auto_link_existing_accounts=True,
            email_claim="email",
            require_email_verified=False,
        )
        with pytest.raises(HTTPException) as exc:
            _enforce_auto_link_safety(provider)
        assert exc.value.status_code == 422
        assert exc.value.detail == AUTO_LINK_REQUIREMENTS_ERROR


class TestSchemaValidation:
    def _create(self, **overrides):
        base = {
            "name": "Test",
            "issuer_url": "https://id.example.com",
            "client_id": "client",
            "client_secret": "secret",
        }
        base.update(overrides)
        return OIDCProviderCreate(**base)

    def test_default_create_uses_email_claim_and_strict(self):
        p = self._create()
        assert p.email_claim == "email"
        assert p.require_email_verified is True

    def test_azure_create_with_custom_claim(self):
        p = self._create(
            email_claim="preferred_username",
            require_email_verified=False,
            auto_link_existing_accounts=True,
        )
        assert p.email_claim == "preferred_username"
        assert p.require_email_verified is False
        assert p.auto_link_existing_accounts is True

    def test_create_rejects_invalid_claim_name(self):
        with pytest.raises(ValidationError):
            self._create(email_claim="bad claim name")

    def test_create_rejects_unsafe_combo(self):
        with pytest.raises(ValidationError) as exc:
            self._create(auto_link_existing_accounts=True, require_email_verified=False)
        assert AUTO_LINK_REQUIREMENTS_ERROR in str(exc.value)

    def test_update_partial_unsafe_combo_rejected_at_schema(self):
        with pytest.raises(ValidationError):
            OIDCProviderUpdate(auto_link_existing_accounts=True, require_email_verified=False)

    def test_update_partial_safe_combo_allowed(self):
        # Sending only email_claim change is fine — combined-state guard
        # handles cross-request cases at the route level.
        p = OIDCProviderUpdate(email_claim="upn")
        assert p.email_claim == "upn"


class TestDeriveOIDCUsernameSeed:
    """Pin the resolution order for auto-created OIDC usernames (#1173).

    Pre-fix users with no email claim got ``oidc_<sha256>``-shaped names —
    opaque + useless to the operator. The fix prefers IdP-provided
    ``preferred_username`` then ``name`` then the raw provider sub.
    """

    def test_email_local_part_wins_when_email_resolved(self):
        # The common case for any IdP that ships a usable email claim.
        seed = _derive_oidc_username_seed(
            claims={"preferred_username": "ignored", "name": "Ignored Too"},
            provider_email="alice@corp.example",
            provider_sub="ABC-XYZ-123-LONG-OPAQUE-SUB",
        )
        assert seed == "alice"

    def test_preferred_username_wins_over_name(self):
        seed = _derive_oidc_username_seed(
            claims={"preferred_username": "alice", "name": "Alice Anderson"},
            provider_email=None,
            provider_sub="some-sub",
        )
        assert seed == "alice"

    def test_name_used_when_preferred_username_missing(self):
        seed = _derive_oidc_username_seed(
            claims={"name": "Bob Builder"},
            provider_email=None,
            provider_sub="some-sub",
        )
        # Spaces stripped by the per-candidate sanitisation.
        assert seed == "BobBuilder"

    def test_falls_through_to_sub_when_all_claims_strip_empty(self):
        # ``preferred_username`` and ``name`` both contain only sanitiser-
        # rejected characters → must fall through to ``provider_sub`` rather
        # than locking in ""
        seed = _derive_oidc_username_seed(
            claims={"preferred_username": "!!!", "name": "@@@"},
            provider_email=None,
            provider_sub="ABCDEFGHIJ",
        )
        assert seed == "ABCDEFGHIJ"

    def test_falls_through_to_sub_when_no_human_claims(self):
        seed = _derive_oidc_username_seed(
            claims={},
            provider_email=None,
            provider_sub="abcdefghij1234567890",
        )
        assert seed == "abcdefghij1234567890"

    def test_truncates_sub_to_30_chars(self):
        long_sub = "x" * 100
        seed = _derive_oidc_username_seed(claims={}, provider_email=None, provider_sub=long_sub)
        assert seed == "x" * 30

    def test_non_string_claim_values_are_skipped(self):
        # Misconfigured IdP ships a list instead of a string — must not
        # crash on .strip(), must fall through to the next candidate.
        seed = _derive_oidc_username_seed(
            claims={"preferred_username": ["a", "b"], "name": 42},
            provider_email=None,
            provider_sub="fallback-sub",
        )
        assert seed == "fallback-sub"


class TestDefaultGroupIdSchema:
    """Pin that ``default_group_id`` is round-tripped through the
    Pydantic schemas. Cross-row validation (does the group exist?)
    happens at the route layer and is covered by integration tests."""

    def _create(self, **overrides):
        base = {
            "name": "Test",
            "issuer_url": "https://id.example.com",
            "client_id": "client",
            "client_secret": "secret",
        }
        base.update(overrides)
        return OIDCProviderCreate(**base)

    def test_create_default_group_defaults_to_none(self):
        assert self._create().default_group_id is None

    def test_create_accepts_default_group_id(self):
        assert self._create(default_group_id=42).default_group_id == 42

    def test_update_accepts_default_group_id(self):
        # ``OIDCProviderUpdate`` allows partial updates including this field.
        p = OIDCProviderUpdate(default_group_id=7)
        assert p.default_group_id == 7
