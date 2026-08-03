from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class SmartSensorThreshold(Base):
    """What counts as wrong for one quantity of one sensor — and whether it is
    currently wrong.

    Configuration and state share a row deliberately. The state describes THIS
    threshold, lives exactly as long as it does, and goes with it by cascade.
    Keeping it in memory instead is what ``_ams_alarm_cooldown`` in ``main.py``
    does, and that dictionary forgets on every restart that it has already rung.

    ``min_value`` and ``max_value`` are independent, not a range: "not above 30"
    and "not below 20" are two different worries and a sensor may have one of
    them. A row with neither is refused by the API — it is an empty demand.
    """

    __tablename__ = "smart_sensor_thresholds"

    id: Mapped[int] = mapped_column(primary_key=True)
    sensor_id: Mapped[int] = mapped_column(ForeignKey("smart_sensors.id", ondelete="CASCADE"), index=True)
    # A key of the measurement registry. Validated at the API, not here: a
    # CHECK constraint would have to be edited by a migration every time the
    # registry grows a row.
    kind: Mapped[str] = mapped_column(String(32))

    min_value: Mapped[float | None] = mapped_column(Float)
    max_value: Mapped[float | None] = mapped_column(Float)
    # How far back inside the limit the reading must come before the alarm
    # clears. Applied on the way OUT only — on the way in it would make a
    # threshold of 30 quietly mean 31.
    deadband: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")

    # ok / above / below.
    state: Mapped[str] = mapped_column(String(8), default="ok", server_default="ok")
    state_since: Mapped[datetime | None] = mapped_column(DateTime)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (Index("ix_smart_sensor_thresholds_sensor_kind", "sensor_id", "kind", unique=True),)
