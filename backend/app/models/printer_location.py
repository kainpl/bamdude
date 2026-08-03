from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
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

    A location may stand inside another: a workshop holds shelves, a shelf holds
    printers, and a sensor usually describes the room rather than one shelf. The
    path is never stored — it is derived on every read, so renaming a parent
    costs nothing and can leave no stale copy behind.
    """

    __tablename__ = "printer_locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    # Case-insensitive identity, and unique WITHIN A PARENT rather than
    # globally: "shelf 1" belongs in every workshop that has shelves.
    name_key: Mapped[str] = mapped_column(String(100), index=True)
    # RESTRICT like every other foreign key onto this table: a place is not
    # removed while something stands in it, children included.
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("printer_locations.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # A backstop, not the guard. On SQLite NULL != NULL, so two roots with the
    # same name slip past it — which is why the check also lives in the route.
    __table_args__ = (Index("ix_printer_locations_parent_name", "parent_id", "name_key", unique=True),)
