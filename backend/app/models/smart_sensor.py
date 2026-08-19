from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base

if TYPE_CHECKING:
    from backend.app.models.printer import Printer
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

    ⚠️ **A sensor is bound to a place OR to a printer, never to both.** The two
    answer the same question — where this reading belongs — and a printer
    already has a location, so holding both would let the sensor claim a place
    its printer is not in, and put it in two lists at once. The routes clear
    one when the other is set; nothing else in the codebase should write these
    two columns independently.

    Binding to a printer is what puts a sensor on that printer's card: an
    enclosure thermometer or a door contact belongs to the machine, not to the
    room. Binding to a place is for the room itself. Either is a real answer,
    which is why the operator picks rather than us guessing from the hardware.
    """

    __tablename__ = "smart_sensors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    location_id: Mapped[int | None] = mapped_column(ForeignKey("printer_locations.id", ondelete="RESTRICT"))
    location: Mapped["PrinterLocation | None"] = relationship(lazy="selectin")
    # ⚠️ SET NULL, not CASCADE — unlike the location's RESTRICT. A sensor is
    # physical hardware that outlives the printer it was taped to: deleting the
    # printer must not delete an adopted device, and refusing to delete a
    # printer because a thermometer points at it would be worse still. The
    # sensor simply becomes unbound and the operator re-points it.
    printer_id: Mapped[int | None] = mapped_column(ForeignKey("printers.id", ondelete="SET NULL"), index=True)
    # ⚠️ ``back_populates``, and no cascade, for a reason the tests pin: on
    # SQLite we never issue ``PRAGMA foreign_keys=ON``, so the SET NULL above is
    # decorative there. The relationship is what actually unbinds the sensor
    # when its printer is deleted — exactly how ``Printer.smart_plugs`` already
    # works. Without it a sensor would keep pointing at a row that is gone.
    printer: Mapped["Printer | None"] = relationship(back_populates="smart_sensors", lazy="selectin")
    zigbee_ieee: Mapped[str] = mapped_column(String(23), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Silence is about the device, not about any one quantity, so it cannot
    # live in a per-quantity threshold row. NULL means "speaking".
    silent_since: Mapped[datetime | None] = mapped_column(DateTime)
    silence_notified_at: Mapped[datetime | None] = mapped_column(DateTime)
