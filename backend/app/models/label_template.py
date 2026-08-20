"""Label designs and the paper they can be laid out on.

Two tables rather than one, because they answer different questions. A template
says what a label looks like; a sheet says how many fit on a page and where.
The six names the label API has always taken split across both: four were
labels all along and two were pages.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class LabelTemplate(Base):
    __tablename__ = "label_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    width_mm: Mapped[float] = mapped_column(Float)
    height_mm: Mapped[float] = mapped_column(Float)
    #: ``rect`` or ``round``. Stored from the start and only ``rect`` is drawn:
    #: Niimbot sells circular stock where a rectangular design loses its
    #: corners, and one column now is cheaper than a migration later.
    shape: Mapped[str] = mapped_column(String(16), default="rect")
    #: The design itself — a list of boxes, validated by ``LabelTemplateSpec``.
    elements: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    #: ⚠️ The name the label API has always accepted, for the four that had one.
    #: NULL for anything a person made. A row that carries one is read-only:
    #: an automation printing the same label for a year must not quietly start
    #: printing a different one because somebody edited the built-in.
    builtin_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def is_builtin(self) -> bool:
        return self.builtin_key is not None


class LabelSheet(Base):
    """A page of labels: the paper, the grid, and nothing about the design.

    ⚠️ **No reference to a template.** "This sheet holds that label" would make
    the template undeletable while a sheet looks at it, and weld one paper
    geometry to one design forever. A sheet states its cell size; printing takes
    a sheet plus a template that fits the cell.
    """

    __tablename__ = "label_sheets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    builtin_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    page_size: Mapped[str] = mapped_column(String(16), default="A4")
    cell_width_mm: Mapped[float] = mapped_column(Float)
    cell_height_mm: Mapped[float] = mapped_column(Float)
    cols: Mapped[int] = mapped_column(Integer)
    rows: Mapped[int] = mapped_column(Integer)
    margin_top_mm: Mapped[float] = mapped_column(Float, default=0.0)
    margin_left_mm: Mapped[float] = mapped_column(Float, default=0.0)
    gap_x_mm: Mapped[float] = mapped_column(Float, default=0.0)
    gap_y_mm: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    @property
    def is_builtin(self) -> bool:
        return self.builtin_key is not None
