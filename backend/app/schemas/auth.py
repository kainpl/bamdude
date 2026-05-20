import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def _validate_password_complexity(v: str) -> str:
    """Enforce minimum password complexity (upstream §18.6 M-C).

    Requires at least one uppercase letter, one lowercase letter, and one
    digit in addition to the min_length=8 Field constraint. The special-
    character rule was dropped — NIST SP 800-63B explicitly advises against
    composition rules beyond length + a basic mix, and the friction was
    causing real operators to just pick worse-remembered passwords.
    Existing stored password hashes are not re-validated — only applies on
    create / change / reset.
    """
    if not re.search(r"[A-Z]", v):
        raise ValueError("Password must contain at least one uppercase letter")
    if not re.search(r"[a-z]", v):
        raise ValueError("Password must contain at least one lowercase letter")
    if not re.search(r"\d", v):
        raise ValueError("Password must contain at least one digit")
    return v


class GroupBrief(BaseModel):
    """Brief group info for embedding in user responses."""

    id: int
    name: str

    class Config:
        from_attributes = True


class LoginRequest(BaseModel):
    username: str = Field(..., max_length=150)
    password: str = Field(..., max_length=256)  # M-NEW-4: cap before pbkdf2
    # Sliding-session refresh token TTL. False (default) → 12 h DB cap + session
    # cookie (dies when the browser closes). True → 30 d DB cap + 30 d cookie
    # Max-Age ("Remember me" checkbox on the login page).
    remember_me: bool = False


class LoginResponse(BaseModel):
    access_token: str | None = None
    token_type: str = "bearer"
    user: "UserResponse | None" = None
    # Set when 2FA is required; the frontend must call /auth/2fa/verify with pre_auth_token.
    requires_2fa: bool = False
    pre_auth_token: str | None = None
    two_fa_methods: list[str] = []


class UserCreate(BaseModel):
    username: str = Field(..., max_length=150)
    password: str | None = Field(default=None, max_length=256)  # M-NEW-4: cap before pbkdf2
    email: str | None = Field(default=None, max_length=254)  # L-NEW-5: RFC 5321 max
    role: str = "user"
    group_ids: list[int] | None = None

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str | None) -> str | None:
        if v is not None:
            _validate_password_complexity(v)
        return v


class UserUpdate(BaseModel):
    username: str | None = Field(default=None, max_length=150)
    password: str | None = Field(default=None, max_length=256)  # M-NEW-4: cap before pbkdf2
    email: str | None = Field(default=None, max_length=254)  # L-NEW-5: RFC 5321 max
    role: str | None = None
    is_active: bool | None = None
    group_ids: list[int] | None = None

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str | None) -> str | None:
        if v is not None:
            _validate_password_complexity(v)
        return v


class UserResponse(BaseModel):
    id: int
    username: str
    email: str | None = None
    role: str  # Deprecated, kept for backward compatibility
    is_active: bool
    is_admin: bool  # Computed from role and group membership
    auth_source: str = "local"  # "local" or "ldap"
    groups: list[GroupBrief] = []
    permissions: list[str] = []  # All permissions from groups
    created_at: str

    class Config:
        from_attributes = True


class LDAPSearchResultResponse(BaseModel):
    """One match from ``GET /auth/ldap/search`` — surfaced in the admin UI."""

    username: str
    email: str | None = None
    display_name: str | None = None
    dn: str
    # True iff this username already exists as a BamDude user (so the UI
    # renders the entry disabled / "already provisioned"). Computed at
    # route time by the search handler — see Bambuddy #1298.
    already_provisioned: bool = False


class LDAPProvisionRequest(BaseModel):
    """Body for ``POST /auth/ldap/provision``. Username is re-resolved via
    the service-account bind, so the request only carries the directory
    username the admin picked from the search results."""

    username: str = Field(..., max_length=150)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., max_length=256)  # M-NEW-3: cap before pbkdf2
    new_password: str = Field(..., min_length=8, max_length=256)

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v: str) -> str:
        return _validate_password_complexity(v)


class SetupRequest(BaseModel):
    admin_username: str = Field(..., max_length=150)
    admin_password: str = Field(..., min_length=8, max_length=256)
    admin_email: str | None = Field(default=None, max_length=254)

    @field_validator("admin_password")
    @classmethod
    def validate_admin_password(cls, v: str) -> str:
        return _validate_password_complexity(v)


class SetupResponse(BaseModel):
    admin_created: bool = True
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class ForgotPasswordRequest(BaseModel):
    email: str = Field(..., max_length=254)  # L-NEW-1: RFC 5321 max; caps memory/CPU before lookup


class ForgotPasswordResponse(BaseModel):
    message: str


class ResetPasswordRequest(BaseModel):
    user_id: int


class ResetPasswordResponse(BaseModel):
    message: str


class SMTPSettings(BaseModel):
    smtp_host: str
    smtp_port: int
    smtp_username: str | None = None  # Optional when auth is disabled
    smtp_password: str | None = None  # Optional for read operations or when auth is disabled
    smtp_security: str = "starttls"  # 'starttls', 'ssl', 'none'
    smtp_auth_enabled: bool = True
    smtp_from_email: str
    smtp_from_name: str = "BamBuddy"
    # Deprecated field for backward compatibility
    smtp_use_tls: bool | None = None


class TestSMTPRequest(BaseModel):
    test_recipient: str


class TestSMTPResponse(BaseModel):
    success: bool
    message: str


class TwoFAStatusResponse(BaseModel):
    totp_enabled: bool
    email_otp_enabled: bool
    backup_codes_remaining: int


class TOTPSetupResponse(BaseModel):
    """Returned when a user initiates TOTP setup.  The frontend should display
    the QR code image (base64 PNG) and ask the user to scan it, then call
    /auth/2fa/totp/enable with a valid code to confirm."""

    secret: str  # base32 secret (shown as fallback text)
    qr_code_b64: str  # base64-encoded PNG of the QR code
    issuer: str


class TOTPSetupRequest(BaseModel):
    """Optional body for POST /auth/2fa/totp/setup.

    Only required when re-initialising setup while an active TOTP record exists.
    Provide the current TOTP code (from the existing authenticator app) to
    confirm intent — mirrors the verification requirement in disable_totp.
    """

    code: str | None = Field(default=None, max_length=8)  # L-NEW-2: bound before pyotp


class TOTPEnableRequest(BaseModel):
    code: str  # 6-digit TOTP code from the authenticator app

    @field_validator("code")
    @classmethod
    def validate_code(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit() or len(v) != 6:
            raise ValueError("TOTP code must be exactly 6 digits")
        return v


class TOTPEnableResponse(BaseModel):
    message: str
    backup_codes: list[str]  # plain-text codes shown once; user must save them


class TOTPDisableRequest(BaseModel):
    """Requires a valid TOTP code OR a backup code to disable TOTP."""

    code: str = Field(..., max_length=128)


class BackupCodesResponse(BaseModel):
    backup_codes: list[str]
    message: str


class EmailOTPEnableRequest(BaseModel):
    """No body required — email is taken from the authenticated user's profile."""

    pass


class TwoFAVerifyRequest(BaseModel):
    pre_auth_token: str = Field(..., max_length=128)
    # TOTP/email codes are 6 digits; backup codes are 8 uppercase alphanumeric chars.
    # max_length=8 prevents excessively long inputs from reaching pbkdf2/pyotp.
    code: str = Field(..., min_length=6, max_length=8)
    method: Literal["totp", "email", "backup"] = "totp"
    # Mirror of LoginRequest.remember_me so the 2-step login flow (password →
    # 2FA → full JWT) can propagate the user's choice to the sliding-session
    # refresh cookie issued on successful verification. Default False matches
    # LoginRequest.
    remember_me: bool = False

    @field_validator("code")
    @classmethod
    def validate_code_format(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^[A-Za-z0-9]{6,8}$", v):
            raise ValueError("Code must be 6–8 alphanumeric characters")
        return v.upper()  # normalise backup codes to uppercase


class TwoFAVerifyResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: "UserResponse"


class EmailOTPSendRequest(BaseModel):
    pre_auth_token: str = Field(..., max_length=128)


class EmailOTPEnableConfirmRequest(BaseModel):
    """Body for the second step of email OTP enable: verify the proof-of-possession code."""

    setup_token: str = Field(..., max_length=128)
    # L-NEW-3: email OTP setup codes are always exactly 6 digits; reject anything else.
    code: str = Field(..., min_length=6, max_length=6)

    @field_validator("code")
    @classmethod
    def validate_code_digits(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit() or len(v) != 6:
            raise ValueError("Email OTP setup code must be exactly 6 digits")
        return v


class EmailOTPDisableRequest(BaseModel):
    """Requires the account password to disable email OTP."""

    password: str = Field(..., max_length=256)


class AdminDisable2FARequest(BaseModel):
    """Admin must supply their own password as re-auth before disabling 2FA for another user.

    OIDC/LDAP-only admins (no local password_hash) are exempt from this check.
    """

    admin_password: str | None = Field(default=None, max_length=256)


# ---------------------------------------------------------------------------
# OIDC schemas
# ---------------------------------------------------------------------------


# Reused error message for both Pydantic model_validator and the route-level
# Combined-State-Guard. Surfaced in 422 responses so the frontend form can
# display it directly when the operator picks an unsafe combination.
AUTO_LINK_REQUIREMENTS_ERROR = (
    "auto_link_existing_accounts requires require_email_verified=True when email_claim='email'"
)


def _validate_email_claim_name(v: str) -> str:
    """Whitelist alphanumeric/underscore/hyphen claim names starting with a letter.

    Operator-supplied claim name flows into log messages and into the dynamic
    ``claims.get(...)`` lookup; constraining it to a safe character set
    prevents log injection and limits the attack surface of a hostile config.
    """
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_\-]{0,63}", v):
        raise ValueError("Invalid claim name")
    return v


def _validate_icon_url(v: str | None) -> str | None:
    """Reject non-HTTPS icon URLs to prevent SSRF / mixed-content issues."""
    if v is None:
        return v
    if not v.startswith("https://"):
        raise ValueError("icon_url must start with https://")
    return v


def _validate_issuer_url(v: str | None) -> str | None:
    """Nit4: Reject non-HTTPS issuer URLs and private/loopback/link-local hosts.

    HTTP is no longer accepted — OIDC providers must be reachable over TLS.
    Private-network and loopback addresses are rejected to prevent SSRF attacks
    where an admin-supplied URL could reach internal services.
    """
    import ipaddress
    from urllib.parse import urlparse

    if v is None:
        return v
    if not v.startswith("https://"):
        raise ValueError("issuer_url must start with https://")
    host = urlparse(v).hostname or ""
    try:
        addr = ipaddress.ip_address(host)
        if addr.is_private or addr.is_loopback or addr.is_link_local:
            raise ValueError("issuer_url must not point to a private, loopback, or link-local address")
    except ValueError as exc:
        if "issuer_url" in str(exc):
            raise
        # hostname is a domain name, not a bare IP — that's fine
    return v


def _validate_scopes(v: str | None) -> str | None:
    """Nit5: Require that the 'openid' scope is present.

    The OpenID Connect spec mandates the 'openid' scope; without it the
    response is plain OAuth2, not OIDC, and claims like sub/email are not
    guaranteed.
    """
    if v is None:
        return v
    scope_list = v.split()
    if "openid" not in scope_list:
        raise ValueError("scopes must include 'openid'")
    return v


class OIDCProviderCreate(BaseModel):
    name: str = Field(..., max_length=100)  # L-NEW-4
    issuer_url: str
    client_id: str = Field(..., max_length=256)  # L-NEW-4
    client_secret: str = Field(..., max_length=512)  # L-NEW-4: Fernet input bounded
    scopes: str = Field(default="openid email profile", max_length=256)  # L-NEW-4
    is_enabled: bool = True
    auto_create_users: bool = False
    auto_link_existing_accounts: bool = False  # M-2: conservative default, opt-in only
    email_claim: str = Field(default="email", max_length=64)
    require_email_verified: bool = True
    icon_url: str | None = None
    # Operator-configurable default group for auto-created OIDC users
    # (#1173). NULL → callback falls back to "Viewers".
    default_group_id: int | None = None

    @field_validator("issuer_url")
    @classmethod
    def validate_issuer_url(cls, v: str) -> str:
        result = _validate_issuer_url(v)
        assert result is not None
        return result

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, v: str) -> str:
        result = _validate_scopes(v)
        assert result is not None
        return result

    @field_validator("email_claim")
    @classmethod
    def validate_email_claim(cls, v: str) -> str:
        return _validate_email_claim_name(v)

    @field_validator("icon_url")
    @classmethod
    def validate_icon_url(cls, v: str | None) -> str | None:
        return _validate_icon_url(v)

    # Only Fall B (email_claim='email' + require_email_verified=False) is unsafe
    # to combine with auto_link — an attacker-controlled IdP could present an
    # unverified email matching a local account. Fall C (custom claim) never
    # consults email_verified, so it's safe regardless of require_email_verified.
    @model_validator(mode="after")
    def check_auto_link_requires_verified(self) -> "OIDCProviderCreate":
        if self.auto_link_existing_accounts and self.email_claim == "email" and not self.require_email_verified:
            raise ValueError(AUTO_LINK_REQUIREMENTS_ERROR)
        return self


class OIDCProviderUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    issuer_url: str | None = None

    @field_validator("issuer_url")
    @classmethod
    def validate_issuer_url(cls, v: str | None) -> str | None:
        return _validate_issuer_url(v)

    client_id: str | None = Field(default=None, max_length=256)
    client_secret: str | None = Field(default=None, max_length=512)
    scopes: str | None = Field(default=None, max_length=256)
    is_enabled: bool | None = None
    auto_create_users: bool | None = None
    auto_link_existing_accounts: bool | None = None
    email_claim: str | None = Field(default=None, max_length=64)
    require_email_verified: bool | None = None
    icon_url: str | None = None
    default_group_id: int | None = None

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, v: str | None) -> str | None:
        return _validate_scopes(v)

    @field_validator("email_claim")
    @classmethod
    def validate_email_claim(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _validate_email_claim_name(v)

    @field_validator("icon_url")
    @classmethod
    def validate_icon_url(cls, v: str | None) -> str | None:
        return _validate_icon_url(v)

    # Schema-level guard catches the unsafe combo only when all three pieces
    # arrive in the same request. email_claim=None means "leave unchanged"
    # (still 'email' by default), so we treat None as 'email' for this check.
    # Partial updates spanning two requests are caught by the
    # Combined-State-Guard in the route after the setattr loop.
    @model_validator(mode="after")
    def check_auto_link_requires_verified(self) -> "OIDCProviderUpdate":
        if (
            self.auto_link_existing_accounts is True
            and self.require_email_verified is False
            and (self.email_claim is None or self.email_claim == "email")
        ):
            raise ValueError(AUTO_LINK_REQUIREMENTS_ERROR)
        return self


class OIDCProviderResponse(BaseModel):
    id: int
    name: str
    issuer_url: str
    client_id: str
    scopes: str
    is_enabled: bool
    auto_create_users: bool
    auto_link_existing_accounts: bool = False
    email_claim: str = "email"
    require_email_verified: bool = True
    icon_url: str | None = None
    # True iff the server has cached image bytes for this provider. The
    # SPA uses this to decide whether to render
    # ``<img src="/api/v1/auth/oidc/providers/{id}/icon">`` vs a fallback
    # avatar — without it the SPA would 404-storm on every login-page
    # render for providers configured with an icon_url that hasn't yet
    # been successfully fetched. Sourced from ``OIDCProvider.has_icon``
    # property (reads ``icon_content_type``, never triggers a deferred-
    # column lazy-load). Upstream Bambuddy #1333.
    has_icon: bool = False
    default_group_id: int | None = None

    class Config:
        from_attributes = True


class OIDCAuthorizeResponse(BaseModel):
    auth_url: str


class OIDCExchangeRequest(BaseModel):
    oidc_token: str = Field(..., max_length=128)


class OIDCLinkResponse(BaseModel):
    id: int
    provider_id: int
    provider_name: str
    provider_email: str | None = None
    created_at: str


class EncryptionRowCounts(BaseModel):
    oidc_providers: int
    user_totp: int


class EncryptionStatusResponse(BaseModel):
    key_configured: bool
    key_source: Literal["env", "file", "generated", "none"]
    legacy_plaintext_rows: EncryptionRowCounts
    encrypted_rows: EncryptionRowCounts
    # Filled by the endpoint after a sample-decrypt of one encrypted row, so
    # a wrong-key state (key_configured=True but rows decrypt to junk) is
    # detected, not just the no-key case.
    decryption_broken: bool = False
    # Number of rows skipped during the last legacy re-encryption migration.
    # Filled from backend.app.core.database.get_migration_error_count().
    migration_error_count: int = 0
