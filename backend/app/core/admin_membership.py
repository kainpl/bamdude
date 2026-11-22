"""Protect the last active administrator across account and group writers."""

from fastapi import HTTPException
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.group import Group
from backend.app.models.user import ADMIN_GROUP_NAME, User

LAST_ADMIN_DEMOTION = "Cannot remove admin rights from the last admin user"


async def require_other_active_admin(db: AsyncSession, user_id: int, detail: str) -> None:
    """Check under the transaction's account guard, before changing authority."""
    if db.get_bind().dialect.name == "postgresql":
        # Account guards cover the target and actor. Reductions on disjoint
        # accounts must also serialize, including directory-driven changes.
        # SQLite already reserves its writer in the account guard.
        await db.execute(text("SELECT pg_advisory_xact_lock(216, 1)"))
    other = await db.scalar(
        select(User.id)
        .where(
            User.id != user_id,
            User.is_active.is_(True),
            or_(User.role == "admin", User.groups.any(Group.name == ADMIN_GROUP_NAME)),
        )
        .limit(1)
    )
    if other is None:
        raise HTTPException(status_code=400, detail=detail)
