from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class ZigbeeDevice(Base):
    """What the radio knows about a paired device — one row per IEEE.

    Deliberately separate from ``smart_plugs`` and ``smart_sensors``. A Zigbee
    plug's radio settings cannot live on ``smart_plugs``: that table also holds
    Tasmota, Home Assistant, MQTT and REST plugs, for which these columns are
    meaningless. And the row must exist *before* the entity row does — a plug is
    paired first and added as a device afterwards, and in that window it is
    already on the mesh and already being configured.

    There is no ``adopted`` column. Adopted means "an entity row references this
    IEEE" (``smart_plugs`` for a plug, ``smart_sensors`` for a sensor); a flag
    beside that would be a second source of truth for one question, and the two
    would eventually disagree with nothing to say which is right.
    """

    __tablename__ = "zigbee_devices"

    ieee: Mapped[str] = mapped_column(String(23), primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))  # plug | sensor
    # What the hardware calls itself. NOT edited by anyone — the operator's own
    # name lives on the entity row (smart_plugs.name / smart_sensors.name).
    name: Mapped[str | None] = mapped_column(String(100))
    # {target_key: {min_interval, max_interval, reportable_change}}. NULL means
    # "no override of its own" and is not the same as an empty object, which
    # would mean "overridden with nothing".
    reporting: Mapped[dict | None] = mapped_column(JSON)
    poll_seconds: Mapped[int | None] = mapped_column(Integer)
    stale_after_seconds: Mapped[int | None] = mapped_column(Integer)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
