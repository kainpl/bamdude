"""Group model for permission-based access control."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Table, false, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from backend.app.core.database import Base

if TYPE_CHECKING:
    from backend.app.models.user import User


# Many-to-many association table between users and groups
user_groups = Table(
    "user_groups",
    Base.metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", Integer, ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
)

# N62: the printers a group with ``restrict_printers`` reaches — picked one by one, and
# every printer standing in one of its locations or anywhere below it. A group's reach is
# worked out by ``core/printer_scope``, never here. SQLite keeps foreign keys off, so the
# group, printer and location deletes remove these rows themselves.
group_printers = Table(
    "group_printers",
    Base.metadata,
    Column("group_id", Integer, ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
    Column("printer_id", Integer, ForeignKey("printers.id", ondelete="CASCADE"), primary_key=True),
)

group_locations = Table(
    "group_locations",
    Base.metadata,
    Column("group_id", Integer, ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
    Column("location_id", Integer, ForeignKey("printer_locations.id", ondelete="CASCADE"), primary_key=True),
)


class Group(Base):
    """Group model for organizing users and assigning permissions.

    Groups contain a list of permissions that are granted to all members.
    Users can belong to multiple groups, and their permissions are additive.
    System groups (Administrators, Operators, Viewers) cannot be deleted.
    """

    __tablename__ = "groups"
    # N07 spec §B: a deleted group's number is never handed out again (m263).
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    permissions: Mapped[list[str]] = mapped_column(JSON, default=list)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    # N62: the group limits its members to the printers in ``group_printers`` and the
    # locations in ``group_locations``. False — the group limits nothing (every group
    # before N62, and every group of permissions).
    restrict_printers: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationship to users through association table
    users: Mapped[list[User]] = relationship(
        "User",
        secondary=user_groups,
        back_populates="groups",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Group {self.name}>"
