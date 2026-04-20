import re

from pydantic import BaseModel, Field, field_validator


def _validate_password_complexity(v: str) -> str:
    """Enforce minimum password complexity (upstream §18.6 M-C).

    Requires at least one uppercase letter, one lowercase letter, one digit,
    and one special character in addition to the min_length=8 Field constraint.
    Existing stored password hashes are not re-validated — only applies on
    create / change / reset.
    """
    if not re.search(r"[A-Z]", v):
        raise ValueError("Password must contain at least one uppercase letter")
    if not re.search(r"[a-z]", v):
        raise ValueError("Password must contain at least one lowercase letter")
    if not re.search(r"\d", v):
        raise ValueError("Password must contain at least one digit")
    if not re.search(r"[^A-Za-z0-9]", v):
        raise ValueError("Password must contain at least one special character")
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


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: "UserResponse"


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
