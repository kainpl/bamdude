"""Durable facts and delivery marker for a credential-contract rollout."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class CredentialRolloutEpisode(Base):
    __tablename__ = "credential_rollout_episodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    contract_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    ownerless_api_key_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    owned_api_key_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    long_lived_token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
