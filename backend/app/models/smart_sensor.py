from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class SmartSensor(Base):
    """A sensor the operator has adopted. Mirrors ``SmartPlug`` on purpose.

    Its existence IS adoption — a paired sensor with no row here stays on the
    mesh, keeps its settings and goes on being configured, but is not something
    the farm shows or acts on. That is the same rule plugs have always had, and
    it is why there is no ``adopted`` flag anywhere.

    ``location`` is a free string with the same meaning as ``Printer.location``
    — a group name the operator types. It is NOT a foreign key to ``locations``,
    which is filament-spool storage and unrelated. Nothing reads this column
    yet; it lands now so the next cycle's location binding needs no migration.
    """

    __tablename__ = "smart_sensors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    location: Mapped[str | None] = mapped_column(String(100))
    zigbee_ieee: Mapped[str] = mapped_column(String(23), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
