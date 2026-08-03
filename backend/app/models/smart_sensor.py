from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base

if TYPE_CHECKING:
    from backend.app.models.printer_location import PrinterLocation


class SmartSensor(Base):
    """A sensor the operator has adopted. Mirrors ``SmartPlug`` on purpose.

    Its existence IS adoption — a paired sensor with no row here stays on the
    mesh, keeps its settings and goes on being configured, but is not something
    the farm shows or acts on. That is the same rule plugs have always had, and
    it is why there is no ``adopted`` flag anywhere.

    ``location_id`` points at ``printer_locations`` — the same lookup table a
    printer points at, so a sensor and the printers around it can be asked
    about together. It is NOT the spool-storage ``locations`` table, which is
    unrelated.
    """

    __tablename__ = "smart_sensors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    location_id: Mapped[int | None] = mapped_column(ForeignKey("printer_locations.id", ondelete="RESTRICT"))
    location: Mapped["PrinterLocation | None"] = relationship(lazy="selectin")
    zigbee_ieee: Mapped[str] = mapped_column(String(23), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
