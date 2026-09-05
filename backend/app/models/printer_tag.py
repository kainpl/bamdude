"""Printer tags — free labels an operator pins on a printer.

An entity with a case-insensitive identity, not a JSON list of strings on the
printer row. The lesson of printer locations applies unchanged: a free string
typed in two dialogs silently disagrees, and the first consumer of tags
(staggered-start groups, ``services/stagger_groups.py``) must be able to say
"these tags" by id. Design: docs/superpowers/specs/2026-09-05-stagger-groups-design.md.

Flat on purpose — a tag has no parent. ``printer_tag_links`` is the join table;
SQLite ignores ON DELETE, so the routes remove links in code
(``services/printer_tag_service.py``).
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class PrinterTag(Base):
    __tablename__ = "printer_tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    # Case-insensitive identity, unique across the farm: "Фаза 1" and "фаза 1"
    # are one tag — the condition this entity exists to end.
    name_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (Index("ix_printer_tags_name_key", "name_key", unique=True),)


class PrinterTagLink(Base):
    __tablename__ = "printer_tag_links"

    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"), primary_key=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("printer_tags.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (Index("ix_printer_tag_links_tag", "tag_id"),)
