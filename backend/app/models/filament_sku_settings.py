"""Per-SKU reorder configuration for the stock-forecasting panel.

Adapted from upstream Bambuddy ``37c9d5f2`` (#1184). The SKU tuple
(material, subtype, brand) keys this table — settings persist even when
zero spools currently exist for the SKU, so the user can pre-configure
lead-time / safety-margin / snooze before the first purchase. The
forecasting algorithm itself runs client-side in ``ForecastPanel.tsx``;
this table only stores the operator's reorder preferences.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class FilamentSkuSettings(Base):
    """User-configured reorder settings for a filament SKU (material/subtype/brand group)."""

    __tablename__ = "filament_sku_settings"
    __table_args__ = (
        # On Postgres standard UNIQUE handles the tuple; SQLite treats NULL as
        # distinct so the constraint is best-effort there (callers upsert via
        # the API endpoint which loads-then-mutates, dodging the race).
        UniqueConstraint("material", "subtype", "brand", name="uq_filament_sku"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    material: Mapped[str] = mapped_column(String(50))
    subtype: Mapped[str | None] = mapped_column(String(50))
    brand: Mapped[str | None] = mapped_column(String(100))
    lead_time_days: Mapped[int] = mapped_column(Integer, default=0)
    safety_margin_value: Mapped[int] = mapped_column(Integer, default=14)
    # "days" → multiplied by daily-rate to form a grams figure; "g" → already grams.
    safety_margin_unit: Mapped[str] = mapped_column(String(10), default="days")
    alerts_snoozed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
