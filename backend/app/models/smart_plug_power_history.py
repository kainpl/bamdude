from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Index, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class SmartPlugPowerHistory(Base):
    """What a plug was drawing, moment by moment.

    Separate from ``smart_plug_energy_snapshots``, which holds the lifetime kWh
    counter hourly and feeds per-print accounting. This one is instantaneous
    watts and feeds nothing but charts — no total is ever derived from it, so a
    gap here costs a drawing and nothing else.

    The key is a BIGINT. Retention bounds how many rows the table holds; it does
    not bound the counter, which only grows, and PostgreSQL's usual SERIAL is
    32-bit. On SQLite this changes nothing: INTEGER PRIMARY KEY is already the
    64-bit rowid.
    """

    __tablename__ = "smart_plug_power_history"
    __table_args__ = (Index("ix_plug_power_history_plug_time", "plug_id", "recorded_at"),)

    # BigInteger everywhere EXCEPT SQLite, where only INTEGER PRIMARY KEY is the
    # rowid alias — a BIGINT primary key there is a plain column that nothing
    # fills in, and every insert fails on NOT NULL. SQLite's rowid is already
    # 64-bit, so the variant costs nothing and PostgreSQL still gets BIGSERIAL.
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    plug_id: Mapped[int] = mapped_column(ForeignKey("smart_plugs.id", ondelete="CASCADE"), nullable=False)
    power: Mapped[float] = mapped_column(Float, nullable=False)  # watts
    recorded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
