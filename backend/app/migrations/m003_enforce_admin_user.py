"""Enforce the always-on auth model.

Prior to this migration BamDude could run in an "opt-out" mode where auth was
disabled entirely and no user existed. The refactor removed that mode; the
system now always requires at least one admin user and every API endpoint
requires authentication.

For existing installs we handle two scenarios in the ``seed`` phase:

* **At least one admin exists** - force ``auth_enabled`` and ``setup_completed``
  both to ``true``. Users can log in with their existing credentials on the
  next boot, nothing else changes.

* **No admin exists** (``auth_enabled=false`` installs that never created a
  user, or anything that somehow lost its last admin) - clear both
  ``setup_completed`` and ``auth_enabled`` so the middleware routes the user
  through ``/setup`` on next boot. Existing non-admin users (if any) are left
  untouched and will remain in place as regular Operator/Viewer members once
  an admin is created.

Schema is unchanged; ``upgrade`` is a no-op.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, func, select

logger = logging.getLogger(__name__)

version = 3
name = "enforce_admin_user"


async def upgrade(conn):  # noqa: ARG001 - interface compat
    """No schema changes."""
    return None


async def seed(session_factory):
    """Reconcile auth/setup flags with actual admin presence."""
    from backend.app.models.group import Group, user_groups
    from backend.app.models.settings import Settings
    from backend.app.models.user import User

    async with session_factory() as db:
        # Count active admins: legacy role='admin' OR member of Administrators group.
        legacy_count = (
            await db.execute(
                select(func.count()).select_from(User).where(User.is_active.is_(True), User.role == "admin")
            )
        ).scalar_one() or 0

        group_count = (
            await db.execute(
                select(func.count())
                .select_from(User)
                .join(user_groups, user_groups.c.user_id == User.id)
                .join(Group, Group.id == user_groups.c.group_id)
                .where(User.is_active.is_(True), Group.name == "Administrators")
            )
        ).scalar_one() or 0

        has_admin = (legacy_count + group_count) > 0

        if has_admin:
            # Normalize the legacy flags to reflect the new invariant.
            await _upsert_setting(db, "auth_enabled", "true")
            await _upsert_setting(db, "setup_completed", "true")
            logger.info(
                "m003: %d admin user(s) detected - auth_enabled/setup_completed forced to true",
                legacy_count + group_count,
            )
        else:
            # Force the setup flow on next boot. Preserve non-admin users (if any).
            await db.execute(delete(Settings).where(Settings.key.in_(["setup_completed", "auth_enabled"])))
            logger.warning(
                "m003: no admin user found - setup_completed/auth_enabled cleared. "
                "Setup will be required on next start.",
            )

        await db.commit()


async def _upsert_setting(db, key: str, value: str) -> None:
    """Insert or update a ``Settings`` row. Dialect-aware for SQLite/PostgreSQL."""
    from backend.app.models.settings import Settings

    existing = (await db.execute(select(Settings).where(Settings.key == key))).scalar_one_or_none()
    if existing is None:
        db.add(Settings(key=key, value=value))
    else:
        existing.value = value
