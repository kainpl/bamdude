"""A browser lifetime, using the existing refresh family as its sole sid."""

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, validates

from backend.app.core.database import Base


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    sid: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_identity: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    auth_source: Mapped[str] = mapped_column(String(20), nullable=False)
    remember_me: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    @validates("created_at", "expires_at", "revoked_at")
    def _utc_datetime(self, _name, value):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value is not None and value.tzinfo else value
