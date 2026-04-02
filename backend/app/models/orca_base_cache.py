"""Cache model for OrcaSlicer base profiles fetched from GitHub."""

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class OrcaBaseProfile(Base):
    """Cached OrcaSlicer base profile from GitHub for inheritance resolution."""

    __tablename__ = "orca_base_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    profile_type: Mapped[str] = mapped_column(String(20))  # filament, machine, process
    setting: Mapped[str] = mapped_column(Text)  # Full JSON
    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (Index("ix_orca_base_profiles_name", "name", unique=True),)
