from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.api.routes.settings import get_external_login_url
from backend.app.core.auth import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ALGORITHM,
    SECRET_KEY,
    Permission,
    RequirePermission,
    _validate_api_key,
    authenticate_user,
    authenticate_user_by_email,
    create_access_token,
    get_current_active_user,
    get_password_hash,
    get_user_by_email,
    get_user_by_username,
    has_any_admin,
    security,
)
from backend.app.core.database import get_db
from backend.app.core.permissions import ALL_PERMISSIONS, DEFAULT_GROUPS
from backend.app.models.group import Group
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.schemas.auth import (
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    GroupBrief,
    LoginRequest,
    LoginResponse,
    ResetPasswordRequest,
    ResetPasswordResponse,
    SetupRequest,
    SetupResponse,
    SMTPSettings,
    TestSMTPRequest,
    TestSMTPResponse,
    UserResponse,
)
from backend.app.services.email_service import (
    create_password_reset_email_from_template,
    generate_secure_password,
    get_smtp_settings,
    save_smtp_settings,
    send_email,
)


def _user_to_response(user: User) -> UserResponse:
    """Convert a User model to UserResponse schema."""
    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        role=user.role,
        is_active=user.is_active,
        is_admin=user.is_admin,
        auth_source=getattr(user, "auth_source", "local"),
        groups=[GroupBrief(id=g.id, name=g.name) for g in user.groups],
        permissions=sorted(user.get_permissions()),
        created_at=user.created_at.isoformat(),
    )


def _api_key_to_user_response(api_key) -> UserResponse:
    """Create a synthetic admin UserResponse for a valid API key."""
    return UserResponse(
        id=0,
        username=f"api-key:{api_key.key_prefix}",
        email=None,
        role="admin",
        is_active=True,
        is_admin=True,
        groups=[],
        permissions=sorted(ALL_PERMISSIONS),
        created_at=api_key.created_at.isoformat(),
    )


router = APIRouter(prefix="/auth", tags=["authentication"])


async def is_advanced_auth_enabled(db: AsyncSession) -> bool:
    """Check if advanced authentication is enabled."""
    result = await db.execute(select(Settings).where(Settings.key == "advanced_auth_enabled"))
    setting = result.scalar_one_or_none()
    if setting is None:
        return False
    return setting.value.lower() == "true"


async def set_advanced_auth_enabled(db: AsyncSession, enabled: bool) -> None:
    """Set advanced authentication enabled status."""
    from sqlalchemy import func
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    stmt = sqlite_insert(Settings).values(key="advanced_auth_enabled", value="true" if enabled else "false")
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"], set_={"value": "true" if enabled else "false", "updated_at": func.now()}
    )
    await db.execute(stmt)


async def is_setup_completed(db: AsyncSession) -> bool:
    """Check if setup has been completed."""
    result = await db.execute(select(Settings).where(Settings.key == "setup_completed"))
    setting = result.scalar_one_or_none()
    return setting and setting.value.lower() == "true"


async def set_setup_completed(db: AsyncSession, completed: bool) -> None:
    """Set setup completed status."""
    from sqlalchemy import func
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    stmt = sqlite_insert(Settings).values(key="setup_completed", value="true" if completed else "false")
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"], set_={"value": "true" if completed else "false", "updated_at": func.now()}
    )
    await db.execute(stmt)
    # Note: Don't commit here - let get_db handle it or commit explicitly in the route


@router.post("/setup", response_model=SetupResponse)
async def setup_auth(request: SetupRequest, db: AsyncSession = Depends(get_db)):
    """First-time setup: create the initial admin user and auto-login.

    Public endpoint - intentionally unauthenticated, because the system has no
    admin yet. Once any admin exists, subsequent requests are rejected with 403.
    """
    import logging

    logger = logging.getLogger(__name__)

    try:
        # Block re-runs once an admin exists. This is the sole gate - we don't
        # read any "setup_completed" flag, because the admin-count is the real
        # source of truth (a stale flag with zero admins would otherwise lock
        # the system out).
        if await has_any_admin(db):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Setup has already been completed. An admin user already exists.",
            )

        if not request.admin_username.strip() or not request.admin_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Admin username and password are required",
            )

        # Username collision guard - safe to run even before setup completes.
        existing_user = await get_user_by_username(db, request.admin_username)
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User with this username already exists",
            )

        # Ensure the "Administrators" system group exists. It is normally seeded
        # by migration m001, but a rescue path (CLI reset_admin + fresh DB) may
        # reach setup before seeds run, so we create it on demand.
        admin_group_result = await db.execute(select(Group).where(Group.name == "Administrators"))
        admin_group = admin_group_result.scalar_one_or_none()
        if admin_group is None:
            admin_group_def = DEFAULT_GROUPS["Administrators"]
            admin_group = Group(
                name="Administrators",
                description=admin_group_def["description"],
                permissions=list(admin_group_def["permissions"]),
                is_system=admin_group_def["is_system"],
            )
            db.add(admin_group)
            await db.flush()
            logger.info("Seeded missing 'Administrators' system group during setup")

        admin_user = User(
            username=request.admin_username.strip(),
            email=request.admin_email.strip() if request.admin_email else None,
            password_hash=get_password_hash(request.admin_password),
            role="admin",
            is_active=True,
        )
        admin_user.groups.append(admin_group)
        db.add(admin_user)

        # Mark setup as completed for UI fast-path. The authoritative check is
        # still has_any_admin(), so this flag is advisory only.
        await set_setup_completed(db, True)
        await db.commit()
        await db.refresh(admin_user)

        # Invalidate the setup-gate cache so subsequent requests are unblocked
        # without a restart.
        try:
            from backend.app.main import invalidate_setup_gate_cache  # local import to avoid cycle

            invalidate_setup_gate_cache()
        except Exception:
            # main.py cache is nice-to-have; tolerate its absence (e.g. during
            # test imports).
            pass

        # Reload with groups populated for the response.
        result = await db.execute(select(User).where(User.id == admin_user.id).options(selectinload(User.groups)))
        admin_user = result.scalar_one()

        access_token = create_access_token(
            data={"sub": admin_user.username},
            expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        )

        logger.info("Initial admin user created during setup: %s", admin_user.username)

        return SetupResponse(
            admin_created=True,
            access_token=access_token,
            token_type="bearer",
            user=_user_to_response(admin_user),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Setup error: %s", e, exc_info=True)
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Setup failed",
        )


@router.get("/status")
async def get_auth_status(db: AsyncSession = Depends(get_db)):
    """Get authentication status (public endpoint).

    ``requires_setup`` is ``True`` iff no active admin user exists. Auth itself
    is always on - the legacy ``auth_enabled`` field is kept as ``True`` for
    backward compatibility with older frontends.
    """
    requires_setup = not await has_any_admin(db)
    return {"auth_enabled": True, "requires_setup": requires_setup}


@router.post("/login", response_model=LoginResponse)
async def login(request: LoginRequest, db: AsyncSession = Depends(get_db)):
    """Login and get access token.

    Supports username or email-based login. Username lookup is case-insensitive.
    """
    # Check if LDAP is enabled
    ldap_user = None
    ldap_settings = await _get_ldap_settings(db)
    if ldap_settings:
        try:
            from backend.app.services.ldap_service import (
                authenticate_ldap_user,
                parse_ldap_config,
            )

            ldap_config = parse_ldap_config(ldap_settings)
            if ldap_config:
                ldap_user = authenticate_ldap_user(ldap_config, request.username, request.password)
                if ldap_user:
                    # LDAP auth succeeded - find or create local user
                    user = await get_user_by_username(db, ldap_user.username)
                    if user and user.auth_source != "ldap":
                        # Username exists as local user - don't override
                        user = None
                        ldap_user = None
                    elif not user:
                        if not ldap_config.auto_provision:
                            # User doesn't exist and auto-provision is off
                            ldap_user = None
                        else:
                            # Auto-provision LDAP user
                            user = await _provision_ldap_user(db, ldap_user, ldap_config)

                    if user and ldap_user:
                        # Update email and group mappings on each login
                        await _sync_ldap_user(db, user, ldap_user, ldap_config)
        except Exception as e:
            import logging

            logging.getLogger(__name__).warning("LDAP authentication error, falling back to local: %s", e)
            ldap_user = None

    # Try username-based authentication (skip if already authenticated via LDAP)
    if not ldap_user:
        user = await authenticate_user(db, request.username, request.password)

    # If username auth failed and advanced auth is enabled, try email-based authentication
    if not user and not ldap_user:
        advanced_auth = await is_advanced_auth_enabled(db)
        if advanced_auth:
            user = await authenticate_user_by_email(db, request.username, request.password)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Reload user with groups for proper permission calculation
    result = await db.execute(select(User).where(User.id == user.id).options(selectinload(User.groups)))
    user = result.scalar_one()

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(data={"sub": user.username}, expires_delta=access_token_expires)

    return LoginResponse(
        access_token=access_token,
        token_type="bearer",
        user=_user_to_response(user),
    )


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    db: AsyncSession = Depends(get_db),
):
    """Get current user information.

    Accepts JWT tokens (via Authorization: Bearer header) and API keys
    (via X-API-Key header or Authorization: Bearer bb_xxx).
    API keys return a synthetic admin user with all permissions.
    """
    import jwt
    from jwt.exceptions import PyJWTError as JWTError

    # Check for API key via X-API-Key header
    if x_api_key:
        api_key = await _validate_api_key(db, x_api_key)
        if api_key:
            return _api_key_to_user_response(api_key)

    # Check for Bearer token (could be JWT or API key)
    if credentials is not None:
        token = credentials.credentials
        # Check if it's an API key (starts with bb_)
        if token.startswith("bb_"):
            api_key = await _validate_api_key(db, token)
            if api_key:
                return _api_key_to_user_response(api_key)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Otherwise treat as JWT
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            username: str = payload.get("sub")
            if username is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Could not validate credentials",
                    headers={"WWW-Authenticate": "Bearer"},
                )
        except JWTError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

        user = await get_user_by_username(db, username)
        if user is None or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        # Reload with groups for proper permission calculation
        result = await db.execute(select(User).where(User.id == user.id).options(selectinload(User.groups)))
        user = result.scalar_one()
        return _user_to_response(user)

    # No credentials provided
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


@router.post("/logout")
async def logout():
    """Logout (client should discard token)."""
    return {"message": "Logged out successfully"}


# Advanced Authentication Endpoints


@router.post("/smtp/test", response_model=TestSMTPResponse)
async def test_smtp_connection(
    test_request: TestSMTPRequest,
    current_user: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
    db: AsyncSession = Depends(get_db),
):
    """Test SMTP connection using saved settings (admin only when auth enabled)."""
    import logging

    logger = logging.getLogger(__name__)

    try:
        smtp_settings = await get_smtp_settings(db)
        if not smtp_settings:
            return TestSMTPResponse(success=False, message="SMTP settings not configured. Save SMTP settings first.")

        # Send test email
        send_email(
            smtp_settings=smtp_settings,
            to_email=test_request.test_recipient,
            subject="BamBuddy SMTP Test",
            body_text="This is a test email from BamBuddy. If you received this, your SMTP settings are working correctly!",
            body_html="<p>This is a test email from <strong>BamBuddy</strong>.</p><p>If you received this, your SMTP settings are working correctly!</p>",
        )

        logger.info(f"Test email sent successfully to {test_request.test_recipient}")
        return TestSMTPResponse(success=True, message="Test email sent successfully")
    except Exception as e:
        logger.error("Failed to send test email: %s", e)
        # Note: `message` is surfaced to the admin in the Test SMTP dialog on purpose —
        # that endpoint is admin-only and the exception text is the diagnostic user needs.
        return TestSMTPResponse(success=False, message=f"Failed to send test email: {str(e)}")


@router.get("/smtp", response_model=SMTPSettings | None)
async def get_smtp_config(
    current_user: User | None = RequirePermission(Permission.SETTINGS_READ),
    db: AsyncSession = Depends(get_db),
):
    """Get SMTP settings (admin only when auth enabled). Password is not returned."""
    smtp_settings = await get_smtp_settings(db)
    if smtp_settings:
        # Don't return password in response
        smtp_settings.smtp_password = None
    return smtp_settings


@router.post("/smtp", response_model=dict)
async def save_smtp_config(
    smtp_settings: SMTPSettings,
    current_user: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
    db: AsyncSession = Depends(get_db),
):
    """Save SMTP settings (admin only when auth enabled)."""
    import logging

    logger = logging.getLogger(__name__)

    try:
        await save_smtp_settings(db, smtp_settings)
        await db.commit()
        logger.info(f"SMTP settings updated by admin user: {current_user.username if current_user else 'anonymous'}")
        return {"message": "SMTP settings saved successfully"}
    except Exception as e:
        await db.rollback()
        logger.error("Failed to save SMTP settings: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save SMTP settings",
        )


@router.post("/advanced-auth/enable", response_model=dict)
async def enable_advanced_auth(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Enable advanced authentication (admin only).

    Requires SMTP settings to be configured and tested first.
    """
    import logging

    logger = logging.getLogger(__name__)

    # Reload user with groups for proper is_admin check
    result = await db.execute(select(User).where(User.id == current_user.id).options(selectinload(User.groups)))
    user = result.scalar_one()

    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can enable advanced authentication",
        )

    # Verify SMTP settings are configured
    smtp_settings = await get_smtp_settings(db)
    if not smtp_settings:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SMTP settings must be configured before enabling advanced authentication",
        )

    try:
        await set_advanced_auth_enabled(db, True)
        await db.commit()
        logger.info(f"Advanced authentication enabled by admin user: {user.username}")
        return {"message": "Advanced authentication enabled successfully", "advanced_auth_enabled": True}
    except Exception as e:
        await db.rollback()
        logger.error("Failed to enable advanced authentication: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to enable advanced authentication",
        )


@router.post("/advanced-auth/disable", response_model=dict)
async def disable_advanced_auth(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Disable advanced authentication (admin only)."""
    import logging

    logger = logging.getLogger(__name__)

    # Reload user with groups for proper is_admin check
    result = await db.execute(select(User).where(User.id == current_user.id).options(selectinload(User.groups)))
    user = result.scalar_one()

    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can disable advanced authentication",
        )

    try:
        await set_advanced_auth_enabled(db, False)
        await db.commit()
        logger.info(f"Advanced authentication disabled by admin user: {user.username}")
        return {"message": "Advanced authentication disabled successfully", "advanced_auth_enabled": False}
    except Exception as e:
        await db.rollback()
        logger.error("Failed to disable advanced authentication: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to disable advanced authentication",
        )


@router.get("/advanced-auth/status")
async def get_advanced_auth_status(db: AsyncSession = Depends(get_db)):
    """Get advanced authentication status."""
    advanced_auth_enabled = await is_advanced_auth_enabled(db)
    smtp_configured = await get_smtp_settings(db) is not None
    return {
        "advanced_auth_enabled": advanced_auth_enabled,
        "smtp_configured": smtp_configured,
    }


@router.post("/forgot-password", response_model=ForgotPasswordResponse)
async def forgot_password(request: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)):
    """Request password reset via email (advanced auth only)."""
    import logging

    logger = logging.getLogger(__name__)

    # Check if advanced auth is enabled
    advanced_auth = await is_advanced_auth_enabled(db)
    if not advanced_auth:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Advanced authentication is not enabled",
        )

    # Get SMTP settings
    smtp_settings = await get_smtp_settings(db)
    if not smtp_settings:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Email service is not configured",
        )

    # Find user by email
    user = await get_user_by_email(db, request.email)

    # Always return success message to prevent email enumeration
    # but only send email if user exists and is not an LDAP user
    if user and user.is_active and user.auth_source != "ldap":
        try:
            # Generate new password
            new_password = generate_secure_password()
            user.password_hash = get_password_hash(new_password)
            await db.commit()

            login_url = await get_external_login_url(db)

            # Send password reset email
            subject, text_body, html_body = await create_password_reset_email_from_template(
                db, user.username, new_password, login_url
            )
            send_email(smtp_settings, user.email, subject, text_body, html_body)

            logger.info(f"Password reset email sent to {user.email}")
        except Exception as e:
            logger.error("Failed to send password reset email: %s", e)
            # Don't reveal error to user for security

    return ForgotPasswordResponse(
        message="If the email address is associated with an account, a password reset email has been sent."
    )


@router.post("/reset-password", response_model=ResetPasswordResponse)
async def reset_user_password(
    request: ResetPasswordRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Reset a user's password and send them an email (admin only, advanced auth only)."""
    import logging

    logger = logging.getLogger(__name__)

    # Reload user with groups for proper is_admin check
    result = await db.execute(select(User).where(User.id == current_user.id).options(selectinload(User.groups)))
    admin_user = result.scalar_one()

    if not admin_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can reset user passwords",
        )

    # Check if advanced auth is enabled
    advanced_auth = await is_advanced_auth_enabled(db)
    if not advanced_auth:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Advanced authentication is not enabled",
        )

    # Get SMTP settings
    smtp_settings = await get_smtp_settings(db)
    if not smtp_settings:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Email service is not configured",
        )

    # Find user to reset
    result = await db.execute(select(User).where(User.id == request.user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    if user.auth_source == "ldap":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot reset password for LDAP users - passwords are managed by the LDAP server",
        )

    if not user.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User does not have an email address configured",
        )

    try:
        # Generate new password
        new_password = generate_secure_password()
        user.password_hash = get_password_hash(new_password)
        await db.commit()

        login_url = await get_external_login_url(db)

        # Send password reset email
        subject, text_body, html_body = await create_password_reset_email_from_template(
            db, user.username, new_password, login_url
        )
        send_email(smtp_settings, user.email, subject, text_body, html_body)

        logger.info(f"Password reset by admin {admin_user.username} for user {user.username}")
        return ResetPasswordResponse(message=f"Password reset email sent to {user.email}")
    except Exception as e:
        await db.rollback()
        logger.error("Failed to reset password for user %s: %s", user.username, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reset password",
        )


# LDAP Authentication Helpers


async def _get_ldap_settings(db: AsyncSession) -> dict[str, str] | None:
    """Get LDAP settings from the database. Returns None if LDAP is not enabled."""
    ldap_keys = [
        "ldap_enabled",
        "ldap_server_url",
        "ldap_bind_dn",
        "ldap_bind_password",
        "ldap_search_base",
        "ldap_user_filter",
        "ldap_security",
        "ldap_group_mapping",
        "ldap_auto_provision",
        "ldap_ca_cert_path",
        "ldap_default_group",
    ]
    result = await db.execute(select(Settings).where(Settings.key.in_(ldap_keys)))
    settings = {s.key: s.value for s in result.scalars().all()}
    if settings.get("ldap_enabled", "false").lower() != "true":
        return None
    return settings


async def _provision_ldap_user(db: AsyncSession, ldap_user, ldap_config) -> User:
    """Create a new local user from LDAP authentication."""
    import logging

    from backend.app.services.ldap_service import resolve_group_mapping

    logger = logging.getLogger(__name__)

    new_user = User(
        username=ldap_user.username,
        email=ldap_user.email,
        password_hash=None,
        role="user",
        auth_source="ldap",
        is_active=True,
    )

    # Map LDAP groups to BamDude groups, falling back to the configured default group
    # when the user is authenticated but has no matching group mapping.
    mapped_group_names = resolve_group_mapping(ldap_user.groups, ldap_config.group_mapping)
    if not mapped_group_names and ldap_config.default_group:
        mapped_group_names = [ldap_config.default_group]
        logger.warning(
            "LDAP user %s has no mapped groups - assigning configured default group '%s'",
            ldap_user.username,
            ldap_config.default_group,
        )
    if mapped_group_names:
        groups_result = await db.execute(select(Group).where(Group.name.in_(mapped_group_names)))
        new_user.groups = list(groups_result.scalars().all())

    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)
    logger.info("Auto-provisioned LDAP user: %s (groups: %s)", new_user.username, mapped_group_names)
    return new_user


async def _sync_ldap_user(db: AsyncSession, user: User, ldap_user, ldap_config) -> None:
    """Sync LDAP user attributes (email, groups) on each login."""
    import logging

    from backend.app.services.ldap_service import resolve_group_mapping

    logger = logging.getLogger(__name__)

    changed = False

    # Update email if changed
    if ldap_user.email and ldap_user.email != user.email:
        user.email = ldap_user.email
        changed = True

    # Sync group mappings - always update to match LDAP state (including revocation).
    # Fall back to the configured default group when the user has no mapped groups,
    # so authenticated LDAP users are never left permission-less.
    mapped_group_names = resolve_group_mapping(ldap_user.groups, ldap_config.group_mapping)
    if not mapped_group_names and ldap_config.default_group:
        mapped_group_names = [ldap_config.default_group]
        logger.warning(
            "LDAP user %s has no mapped groups - assigning configured default group '%s'",
            user.username,
            ldap_config.default_group,
        )
    if mapped_group_names:
        groups_result = await db.execute(select(Group).where(Group.name.in_(mapped_group_names)))
        new_groups = list(groups_result.scalars().all())
    else:
        new_groups = []
    current_group_ids = {g.id for g in user.groups}
    new_group_ids = {g.id for g in new_groups}
    if current_group_ids != new_group_ids:
        user.groups = new_groups
        changed = True

    if changed:
        await db.commit()
        logger.info("Synced LDAP user attributes: %s", user.username)


@router.post("/ldap/test")
async def test_ldap(
    current_user: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
    db: AsyncSession = Depends(get_db),
):
    """Test LDAP connection using saved settings (admin only when auth enabled)."""
    import logging

    from backend.app.services.ldap_service import parse_ldap_config, test_ldap_connection

    logger = logging.getLogger(__name__)

    ldap_settings = await _get_ldap_settings(db)
    if not ldap_settings:
        # LDAP might not be enabled yet but settings might still exist - read all keys
        ldap_keys = [
            "ldap_enabled",
            "ldap_server_url",
            "ldap_bind_dn",
            "ldap_bind_password",
            "ldap_search_base",
            "ldap_user_filter",
            "ldap_security",
            "ldap_group_mapping",
            "ldap_auto_provision",
            "ldap_default_group",
        ]
        result = await db.execute(select(Settings).where(Settings.key.in_(ldap_keys)))
        ldap_settings = {s.key: s.value for s in result.scalars().all()}
        # Force enabled for test
        ldap_settings["ldap_enabled"] = "true"

    config = parse_ldap_config(ldap_settings)
    if not config:
        return {"success": False, "message": "LDAP server URL is not configured"}

    success, message = test_ldap_connection(config)
    if success:
        logger.info("LDAP connection test successful")
    else:
        logger.warning("LDAP connection test failed: %s", message)
    return {"success": success, "message": message}


@router.get("/ldap/status")
async def get_ldap_status(db: AsyncSession = Depends(get_db)):
    """Get LDAP authentication status."""
    # Only fetch the minimum keys needed - never load secrets
    ldap_keys = ["ldap_enabled", "ldap_server_url"]
    result = await db.execute(select(Settings).where(Settings.key.in_(ldap_keys)))
    settings = {s.key: s.value for s in result.scalars().all()}
    return {
        "ldap_enabled": settings.get("ldap_enabled", "false").lower() == "true",
        "ldap_configured": bool(settings.get("ldap_server_url")),
    }
