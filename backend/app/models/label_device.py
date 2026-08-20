"""Direct-to-device label printing: the printer, the queue and the cassette map.

A container cannot reach a USB printer, but the desktop process can reach the
container. So the server owns a queue and the desktop asks for work over plain
HTTP. No device protocol lives here — that is the bridge's side.

``driver`` exists from the first row rather than after the second device type
appears: it is one column now and a table rename later.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class LabelDevice(Base):
    """A label printer attached to somebody's desktop, reached through a bridge."""

    __tablename__ = "label_devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Generated once by the bridge and stored in its settings file. Regenerating
    #: it orphans the paired row, which is why the bridge is forbidden to.
    installation_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    driver: Mapped[str] = mapped_column(String(32), default="niimbot")
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    protocol_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transport: Mapped[str | None] = mapped_column(String(16), nullable=True)
    address: Mapped[str | None] = mapped_column(String(128), nullable=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: ⚠️ Pairing, not trust. An API key proves the caller is a bridge; it does
    #: not decide that this particular printer should be given our labels. A
    #: device that has only ever polled stays False until a person adopts it.
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    density: Mapped[int] = mapped_column(Integer, default=3)
    app_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: What the bridge last said was loaded. The barcode is the device's own
    #: word; the two millimetre columns are what the catalogue made of it.
    cassette_barcode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cassette_width_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    cassette_height_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    paper_state: Mapped[int | None] = mapped_column(Integer, nullable=True)
    power_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The bridge is answering but the printer behind it is not. Two different
    #: failures with two different fixes, so they are two different fields.
    printer_reachable: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class LabelJob(Base):
    """One label, already drawn, waiting for a device to come and take it."""

    __tablename__ = "label_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(Integer, ForeignKey("label_devices.id", ondelete="CASCADE"), index=True)
    spool_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Which design drew it. ⚠️ Informational: the PNG below is what prints, and
    #: a template edited after the job was queued must not change it. Nullable
    #: because the row survives the template being deleted.
    template_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    width_mm: Mapped[float] = mapped_column(Float)
    height_mm: Mapped[float] = mapped_column(Float)
    copies: Mapped[int] = mapped_column(Integer, default=1)
    #: ⚠️ Stored at enqueue, never recomputed on claim. A job must print what the
    #: operator previewed, even if the spool is renamed or the design moved in
    #: between — the queue can sit for hours on a desktop that is switched off.
    image_png: Mapped[bytes] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class LabelCassette(Base):
    """Barcode → stock size.

    The device reports what its RFID says and nothing about how big it is, so
    somebody has to hold the map. Keyed by barcode because that is the only
    thing the printer knows; the size is ours to record.
    """

    __tablename__ = "label_cassettes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    barcode: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    width_mm: Mapped[float] = mapped_column(Float)
    height_mm: Mapped[float] = mapped_column(Float)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
