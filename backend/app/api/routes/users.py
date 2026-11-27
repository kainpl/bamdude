import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.api.routes.settings import get_external_login_url
from backend.app.core.admin_membership import LAST_ADMIN_DEMOTION, require_other_active_admin
from backend.app.core.auth import (
    RequestCredentials,
    RequireAdmin,
    RequireAnyPermission,
    RequirePermission,
    get_current_user_optional,
    hash_password_async,
    request_credentials,
    verify_password_async,
)
from backend.app.core.auth_lifetime import (
    capture_account,
    guard_accounts,
    stage_end_sessions,
    utcnow,
    validate_captured,
)
from backend.app.core.auth_runtime import auth_runtime
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.archive import PrintArchive
from backend.app.models.group import Group
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.settings import Settings
from backend.app.models.user import ADMIN_GROUP_NAME, User
from backend.app.schemas.auth import (
    ChangePasswordRequest,
    GroupBrief,
    UserCreate,
    UserResponse,
    UserSlim,
    UserUpdate,
)
from backend.app.services.email_service import (
    create_welcome_email_from_template,
    generate_secure_password,
    get_smtp_settings,
    send_email,
)

router = APIRouter(prefix="/users", tags=["users"])


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


@router.get("", response_model=list[UserResponse])
@router.get("/", response_model=list[UserResponse])
async def list_users(
    _: User | None = RequirePermission(Permission.USERS_READ),
    db: AsyncSession = Depends(get_db),
):
    """List all users."""
    result = await db.execute(select(User).options(selectinload(User.groups)).order_by(User.created_at))
    users = result.scalars().all()
    return [_user_to_response(user) for user in users]


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
@router.post("/", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    user_data: UserCreate,
    _admin: User = RequireAdmin(),
    _: User | None = RequirePermission(Permission.USERS_CREATE),
    db: AsyncSession = Depends(get_db),
):
    """Create a new user. Admin-only (privilege-escalation hardening).

    When advanced authentication is enabled:
    - Email is required
    - Password is auto-generated and emailed to user
    - Admin cannot set or see the password
    """
    import logging

    logger = logging.getLogger(__name__)

    # Check if advanced auth is enabled
    result = await db.execute(select(Settings).where(Settings.key == "advanced_auth_enabled"))
    advanced_auth_setting = result.scalar_one_or_none()
    advanced_auth_enabled = advanced_auth_setting and advanced_auth_setting.value.lower() == "true"

    # Check if username already exists (case-insensitive). Any match means it
    # exists — ``scalar_one_or_none()`` would raise ``MultipleResultsFound`` (a
    # 500 instead of this refusal) on an install that already holds two rows
    # differing only by case, which the pre-Unicode-fold check allowed in.
    existing_user = await db.execute(select(User).where(func.lower(User.username) == func.lower(user_data.username)))
    if existing_user.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already exists",
        )

    # Validate role
    if user_data.role not in ["admin", "user"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Role must be 'admin' or 'user'",
        )

    # Advanced auth validation
    if advanced_auth_enabled:
        if not user_data.email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email is required when advanced authentication is enabled",
            )
        # Check if email already exists (case-insensitive) — any match means it exists, as above.
        existing_email = await db.execute(select(User).where(func.lower(User.email) == func.lower(user_data.email)))
        if existing_email.scalars().first():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already exists",
            )

    # Generate password if advanced auth enabled, otherwise require password
    if advanced_auth_enabled:
        password = generate_secure_password()
    else:
        if not user_data.password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Password is required when advanced authentication is disabled",
            )
        password = user_data.password

    await db.rollback()
    hashed_password = await hash_password_async(password)
    proof = await guard_accounts(db, [_admin._auth_capture])
    actor = await validate_captured(
        db, _admin._auth_capture, required_permissions=(Permission.USERS_CREATE.value,), proof=proof
    )
    if not actor.user.is_admin:
        raise HTTPException(403, "Administrator required")
    new_user = User(
        username=user_data.username,
        email=user_data.email,
        password_hash=hashed_password,
        role=user_data.role,
        is_active=True,
    )

    # Handle group assignments
    if user_data.group_ids:
        groups_result = await db.execute(select(Group).where(Group.id.in_(user_data.group_ids)))
        groups = groups_result.scalars().all()
        if len(groups) != len(user_data.group_ids):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="One or more group IDs are invalid",
            )
        new_user.groups = list(groups)

    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    reply = _user_to_response(new_user)
    # Send welcome email if advanced auth enabled
    if advanced_auth_enabled and new_user.email:
        try:
            smtp_settings = await get_smtp_settings(db)
            if smtp_settings:
                login_url = await get_external_login_url(db)
                subject, text_body, html_body = await create_welcome_email_from_template(
                    db, new_user.username, password, login_url
                )
                recipient = new_user.email
                await db.rollback()
                await asyncio.shield(
                    auth_runtime.spawn(
                        asyncio.to_thread(send_email, smtp_settings, recipient, subject, text_body, html_body)
                    )
                )
                logger.info("Welcome email sent to %s", recipient)
            else:
                logger.warning(f"SMTP not configured, could not send welcome email to {new_user.email}")
        except Exception as e:
            logger.error(f"Failed to send welcome email: {e}")
            # Don't fail user creation if email fails

    return reply


@router.get("/slim", response_model=list[UserSlim])
async def list_users_slim(
    _: User | None = RequireAnyPermission(Permission.USERS_READ_SLIM, Permission.USERS_READ),
    db: AsyncSession = Depends(get_db),
):
    """List users as ``{id, username}`` only.

    Exists so an API key — or a group that should not see emails, roles and
    permission sets — can turn the ``created_by_id`` values it already gets
    back from archives, stats and the queue into names. Without it the only
    user listing is the administrative one, so an operator granted
    ``stats:filter_by_user`` but not ``users:read`` got an empty filter with no
    indication why.

    ``USERS_READ`` is accepted alongside ``USERS_READ_SLIM`` because it is
    strictly broader; groups that already hold it keep working without a
    permission backfill. For API keys only the slim permission resolves (the
    full one is unmapped = administrative), so a key reaches this and not the
    listing above.

    ⚠️ Declared before ``/{user_id}`` on purpose: FastAPI matches in
    declaration order, and the reverse order would parse "slim" as the int
    path parameter and answer 422.
    """
    result = await db.execute(select(User.id, User.username).order_by(User.username))
    return [UserSlim(id=row.id, username=row.username) for row in result.all()]


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: int,
    _: User | None = RequirePermission(Permission.USERS_READ),
    db: AsyncSession = Depends(get_db),
):
    """Get a user by ID."""
    result = await db.execute(select(User).where(User.id == user_id).options(selectinload(User.groups)))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    return _user_to_response(user)


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    user_data: UserUpdate,
    _admin: User = RequireAdmin(),
    _: User | None = RequirePermission(Permission.USERS_UPDATE),
    db: AsyncSession = Depends(get_db),
):
    """Update a user. Admin-only (privilege-escalation hardening)."""
    replacement_hash = await hash_password_async(user_data.password) if user_data.password is not None else None
    result = await db.execute(select(User).where(User.id == user_id).options(selectinload(User.groups)))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    target_capture = capture_account(user)
    proof = await guard_accounts(db, [_admin._auth_capture, target_capture], require_active=False)
    actor = await validate_captured(
        db, _admin._auth_capture, required_permissions=(Permission.USERS_UPDATE.value,), proof=proof
    )
    if not actor.user.is_admin:
        raise HTTPException(403, "Administrator required")
    user = (
        await db.execute(
            select(User)
            .where(User.account_identity == target_capture.account_identity)
            .options(selectinload(User.groups))
            .execution_options(populate_existing=True)
        )
    ).scalar_one()

    new_role = user.role if user_data.role is None else user_data.role
    if user_data.role is not None and new_role not in ("admin", "user"):
        raise HTTPException(400, "Role must be 'admin' or 'user'")

    new_groups = user.groups
    if user_data.group_ids is not None:
        groups = await db.scalars(select(Group).where(Group.id.in_(user_data.group_ids)))
        new_groups = list(groups.all())
        if len(new_groups) != len(user_data.group_ids):
            raise HTTPException(400, "One or more group IDs are invalid")
        if any(g.name == ADMIN_GROUP_NAME for g in user.groups) and not any(
            g.name == ADMIN_GROUP_NAME for g in new_groups
        ):
            # The edit form may echo the legacy role read before the edit.
            # Explicit removal from Administrators revokes both admin grants.
            new_role = "user"
    elif user_data.role == "user":
        # An explicit role-only demotion removes group authority as well.
        new_groups = [g for g in user.groups if g.name != ADMIN_GROUP_NAME]

    stays_active = user.is_active if user_data.is_active is None else user_data.is_active
    stays_admin = new_role == "admin" or any(g.name == ADMIN_GROUP_NAME for g in new_groups)
    if user.is_active and user.is_admin and not (stays_active and stays_admin):
        await require_other_active_admin(
            db,
            user.id,
            "Cannot deactivate the last admin user" if not stays_active else LAST_ADMIN_DEMOTION,
        )

    if (
        user_data.password is not None
        or (user_data.username is not None and user_data.username != user.username)
        or (user_data.is_active is False and user.is_active)
    ):
        await stage_end_sessions(db, target_capture, "account_changed")

    if user_data.username is not None:
        # Check if new username already exists (case-insensitive) — any match means it exists,
        # so this reads the first row rather than raising on two case variants (see create_user).
        existing_user = await db.execute(
            select(User).where(func.lower(User.username) == func.lower(user_data.username), User.id != user_id)
        )
        if existing_user.scalars().first():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Username already exists",
            )
        user.username = user_data.username

    if "email" in user_data.model_fields_set:
        # Check if new email already exists (case-insensitive) — any match means it exists, as above.
        email = user_data.email.strip() if user_data.email else None
        if email:
            existing_email = await db.execute(
                select(User).where(func.lower(User.email) == func.lower(email), User.id != user_id)
            )
            if existing_email.scalars().first():
                raise HTTPException(status_code=400, detail="Email already exists")
        user.email = email or None

    if user_data.password is not None:
        if getattr(user, "auth_source", "local") == "ldap":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot set password for LDAP users",
            )
        user.password_hash = replacement_hash
        user.password_changed_at = utcnow()

    user.role = new_role
    if user_data.is_active is not None:
        user.is_active = user_data.is_active
    if new_groups is not user.groups:
        user.groups = new_groups

    await db.commit()
    result = await db.execute(select(User).where(User.id == user_id).options(selectinload(User.groups)))
    user = result.scalar_one()

    return _user_to_response(user)


@router.get("/{user_id}/items-count")
async def get_user_items_count(
    user_id: int,
    _: User | None = RequirePermission(Permission.USERS_READ),
    db: AsyncSession = Depends(get_db),
):
    """Get count of items created by this user."""
    # Verify user exists
    result = await db.execute(select(User).where(User.id == user_id))
    if not result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Count archives
    archives_result = await db.execute(select(func.count(PrintArchive.id)).where(PrintArchive.created_by_id == user_id))
    archives_count = archives_result.scalar() or 0

    # Count queue items
    queue_result = await db.execute(
        select(func.count(PrintQueueItem.id)).where(PrintQueueItem.created_by_id == user_id)
    )
    queue_items_count = queue_result.scalar() or 0

    # Count library files
    library_result = await db.execute(select(func.count(LibraryFile.id)).where(LibraryFile.created_by_id == user_id))
    library_files_count = library_result.scalar() or 0

    from backend.app.models.auto_queue import AutoQueueItem

    auto_count = await db.scalar(select(func.count(AutoQueueItem.id)).where(AutoQueueItem.created_by_id == user_id))

    return {
        "archives": archives_count,
        "queue_items": queue_items_count,
        "library_files": library_files_count,
        "auto_queue_items": auto_count or 0,
    }


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int,
    delete_items: bool = Query(False, description="Delete all items created by this user"),
    _admin: User = RequireAdmin(),
    current_user: User | None = RequirePermission(Permission.USERS_DELETE),
    db: AsyncSession = Depends(get_db),
    creds: RequestCredentials = Depends(request_credentials),
):
    """Delete a user; owned content uses a bounded atomic domain plan."""
    from fastapi.responses import JSONResponse

    from backend.app.services.user_content_delete import delete_content, detach_account_rows

    user = await db.scalar(select(User).where(User.id == user_id).options(selectinload(User.groups)))
    if user is None:
        raise HTTPException(404, "User not found")
    target = capture_account(user)
    if delete_items:
        result = await delete_content(db, target, _admin, current_user, creds, _check_delete_target)
        return JSONResponse(result, status_code=200)
    proof = await guard_accounts(db, [_admin._auth_capture, target], require_active=False)
    actor = await validate_captured(
        db, _admin._auth_capture, required_permissions=(Permission.USERS_DELETE.value,), proof=proof
    )
    if not actor.user.is_admin:
        raise HTTPException(403, "Administrator required")
    await _check_delete_target(db, user, current_user)
    await stage_end_sessions(db, target, "account_deleted")
    # N62: a limited author's waiting Auto Queue jobs wait for an administrator once they have no
    # author — without this they would read as "nobody's" and route over the whole farm.
    from backend.app.core.printer_scope import hold_departing_author

    await hold_departing_author(db, user)
    await detach_account_rows(db, target)
    # The explicit user-edge policy already removed the association rows;
    # a loaded groups collection must not DELETE those rows a second time.
    await db.execute(delete(User).where(User.account_identity == target.account_identity))
    await db.commit()


async def _check_delete_target(db, user, current_user):
    if user.is_active and user.is_admin:
        await require_other_active_admin(db, user.id, "Cannot delete the last admin user")

    # Prevent deleting yourself (only if auth is enabled and we have a current user)
    if current_user and user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account",
        )


@router.post("/me/change-password", response_model=dict)
async def change_own_password(
    password_data: ChangePasswordRequest,
    current_user: User | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    if current_user is None:
        raise HTTPException(401, "Authentication required to change password")
    if current_user.auth_source == "ldap":
        raise HTTPException(400, "Cannot change password for LDAP users - passwords are managed by the LDAP server")
    expected_hash = current_user.password_hash
    if not expected_hash:
        raise HTTPException(400, "Account has no local password set")
    if not await verify_password_async(password_data.current_password, expected_hash):
        raise HTTPException(400, "Current password is incorrect")
    replacement = await hash_password_async(password_data.new_password)
    captured = current_user._auth_capture
    proof = await guard_accounts(db, [captured])
    principal = await validate_captured(db, captured, proof=proof)
    if principal.user.password_hash != expected_hash:
        raise HTTPException(400, "Current password is incorrect")
    await stage_end_sessions(db, capture_account(principal.user), "password_changed")
    principal.user.password_hash = replacement
    principal.user.password_changed_at = utcnow()
    await db.commit()
    return {"message": "Password changed successfully"}
