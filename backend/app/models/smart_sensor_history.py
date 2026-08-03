from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class SmartSensorHistory(Base):
    """What a sensor measured, one row per quantity per reading.

    Long format, like ``printer_sensor_history``: a new quantity — CO2, PM2.5 —
    becomes a row rather than a migration. The set of quantities belongs to the
    measurement registry and grows there; this table must not have to be altered
    when it does.

    See ``smart_plug_power_history`` for why the key is a BIGINT.
    """

    __tablename__ = "smart_sensor_history"
    __table_args__ = (Index("ix_sensor_history_sensor_kind_time", "sensor_id", "sensor_kind", "recorded_at"),)

    # BigInteger everywhere EXCEPT SQLite, where only INTEGER PRIMARY KEY is the
    # rowid alias — a BIGINT primary key there is a plain column that nothing
    # fills in, and every insert fails on NOT NULL. SQLite's rowid is already
    # 64-bit, so the variant costs nothing and PostgreSQL still gets BIGSERIAL.
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    sensor_id: Mapped[int] = mapped_column(ForeignKey("smart_sensors.id", ondelete="CASCADE"), nullable=False)
    sensor_kind: Mapped[str] = mapped_column(String(32), nullable=False)  # temperature | humidity | battery | ...
    value: Mapped[float] = mapped_column(Float, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
