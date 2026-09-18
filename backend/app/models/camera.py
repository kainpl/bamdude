from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base

if TYPE_CHECKING:
    from backend.app.models.printer_location import PrinterLocation


class Camera(Base):
    """A camera that belongs to no printer — a room, a shelf, a dryer.

    ⚠️ **Not the same thing as a printer's external camera.** Those live as
    columns on ``printers`` and REPLACE that printer's built-in camera: the
    finish photo, the plate check, Obico and the layer timelapse all read
    through them. A row here is nothing of the sort. It is a view of a place,
    and BamDude only ever shows it: the camera wall (signed-in and kiosk), a
    button on its location's group header, a floating window. No frame of it
    ever reaches a printer's consumers, and it never takes the chamber light —
    the light lease keys on a printer, and a room has none.

    ``location_id`` points at ``printer_locations`` — the same lookup table a
    printer and an adopted sensor point at, so "what is in this room" has one
    answer. It is optional: a camera nobody filed still shows on the wall. The
    FK is ``RESTRICT`` like the sensor's, so the location delete route refuses
    while a camera is filed under it and the operator moves it first (⚠️ SQLite
    never enables foreign keys here, so that refusal is the route's guard in
    ``routes/printer_locations.py``, not the database's).

    ``url`` is a URL for mjpeg / rtsp / snapshot and a device path such as
    ``/dev/video0`` for usb — exactly the shapes ``services/external_camera``
    already opens. ``snapshot_url`` is the same optional single-frame override
    a printer's external camera has (#1177): when set, one-shot captures fetch
    it by plain GET instead of reading past the stream's warm-up frame.
    """

    __tablename__ = "cameras"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    camera_type: Mapped[str] = mapped_column(String(20))  # mjpeg, rtsp, snapshot, usb
    url: Mapped[str] = mapped_column(String(500))
    snapshot_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    rotation: Mapped[int] = mapped_column(default=0)  # 0, 90, 180, 270 degrees
    # Off means "configured but not shown": the tile leaves the wall and the
    # location's button disappears, while the row keeps its URL for later. A
    # disabled camera also refuses its stream and snapshot routes, so a kiosk
    # page left open cannot keep pulling from a camera the operator retired.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    location_id: Mapped[int | None] = mapped_column(ForeignKey("printer_locations.id", ondelete="RESTRICT"), index=True)
    location: Mapped["PrinterLocation | None"] = relationship(lazy="selectin")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
