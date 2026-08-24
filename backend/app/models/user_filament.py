"""User tier of the filament catalog (spec A §1): mirrors of PRIVATE cloud
presets of both clouds + link rows for imported local presets, and the
families that are not in the system catalog (custom P-hashes). The SYSTEM
tier deliberately lives outside the DB — backend/app/data/filament_catalog/.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class UserFilamentPreset(Base):
    __tablename__ = "user_filament_presets"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "ecosystem", "cloud_id", name="uq_user_fila_preset_cloud"),
        # cloud_id is NULL for source='local' rows and NULLs never collide
        # (SQLite and PG both), so local rows carry their own key:
        UniqueConstraint("local_preset_id", name="uq_user_fila_preset_local"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,  # NULL = auth-disabled global scope
    )
    ecosystem: Mapped[str] = mapped_column(String(10))  # bambu | orca
    source: Mapped[str] = mapped_column(String(12))  # cloud_bambu | cloud_orca | local
    cloud_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    local_preset_id: Mapped[int | None] = mapped_column(
        ForeignKey("local_presets.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(300))
    # Resolved family; NULL = unresolved (legacy fallback applies, UI flags it).
    family_filament_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    base_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)  # bambu base_id / orca inherits name
    vendor: Mapped[str | None] = mapped_column(String(200), nullable=True)
    filament_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    nozzle_temp_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    nozzle_temp_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_time: Mapped[str | None] = mapped_column(String(40), nullable=True)  # cloud timestamp verbatim
    synced_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # Spec B §5 — Bambu push metadata (m151). pushed_cloud_id is the PFUS id
    # the cloud returned; NULL = not pushed (or the cloud copy vanished).
    pushed_cloud_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    push_dirty: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Orca half of the per-ecosystem push bookkeeping (m154) — a row can be
    # pushed to BOTH clouds. The anchor holds the SERVER updated_time of our
    # last push: the only honest reference for "cloud changed since".
    orca_pushed_profile_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    orca_pushed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    orca_push_dirty: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    orca_pushed_updated_time: Mapped[int | None] = mapped_column(Integer, nullable=True)


class UserFilamentFamily(Base):
    __tablename__ = "user_filament_families"
    __table_args__ = (UniqueConstraint("ecosystem", "filament_id", name="uq_user_fila_family"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    filament_id: Mapped[str] = mapped_column(String(50))
    ecosystem: Mapped[str] = mapped_column(String(10))  # bambu | orca | local ('local' = spec B authoring)
    alias: Mapped[str] = mapped_column(String(200))
    vendor: Mapped[str | None] = mapped_column(String(200), nullable=True)
    filament_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    origin: Mapped[str] = mapped_column(String(12))  # cloud_bambu | cloud_orca | authored | local
    # Set instead of deleting while spools / filament_calibration still
    # reference the filament_id (spec A §3 delete rule).
    orphaned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
