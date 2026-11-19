from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class EnergyPriceHistory(Base):
    """One change of the electricity price (m245).

    The price of a moment is the row with the newest ``effective_at`` at or before it; the
    ``baseline`` row lies far in the past, so every moment before the upgrade resolves to the
    price that stood on the upgrade day. Rows are only appended, and only by
    ``services/energy_price.py``: energy is costed at the price of its own time, so a row that
    changed would re-cost the past.
    """

    __tablename__ = "energy_price_history"
    __table_args__ = (
        CheckConstraint("price_per_kwh >= 0", name="ck_energy_price_history_price"),
        CheckConstraint(
            "source IN ('baseline', 'manual', 'api', 'homeassistant', 'restore')",
            name="ck_energy_price_history_source",
        ),
        Index("ix_energy_price_history_effective_at", "effective_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Naive UTC, like the counter snapshots it is compared with.
    effective_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    price_per_kwh: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
