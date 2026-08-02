from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class PrinterLocation(Base):
    """A place on the farm — a room, a shelf, a wall of printers.

    Distinct from ``locations``, which is filament-spool storage. A drybox is
    not a workshop, and sharing a table because the word matches would merge two
    unrelated ideas into one.

    ``name_key`` is the case-insensitive identity, mirroring the spool-storage
    service exactly. Without it "Цех 2" and "цех 2" are two places, which is the
    condition this entity exists to end.
    """

    __tablename__ = "printer_locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    name_key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
