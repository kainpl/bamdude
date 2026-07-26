"""Saved slice preset bundle — a named, reusable set of the four SliceModal slot picks
(printer / process / filament(s) / bed), loaded in one click instead of re-picking them.

Originally ported from upstream Bambuddy #1425 as "Slicer Pipelines", which also carried
a dispatch target, a fanout strategy and a run/copies engine. That half was **removed**:
BamDude already routes copies across a printer class through its own two-tier queue
(see ``models/auto_queue.py`` — upstream has no auto-queue at all, which is why their
pipelines had to do the fanout themselves). What survives is the part AutoQueue
structurally cannot provide, because AutoQueue's input is an already-sliced artifact:
the slice recipe itself, saved and repeatable.

Table and column names stay ``slicer_pipeline*`` — renaming would cost a data migration
for zero behavioural gain, and the upstream lineage is worth keeping legible.

Soft-deleted (``is_deleted``) rather than hard-deleted so a bundle referenced from
somewhere still resolves its metadata after the operator "deletes" it.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class SlicerPipeline(Base):
    __tablename__ = "slicer_pipelines"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # Preset slots — each is a source-aware PresetRef {source, id}. ``source`` is one of
    # BamDude's four tiers (local / orca_cloud / cloud / standard); ``id`` is the opaque
    # source-specific preset id. Filament slots are a JSON array (one entry per AMS slot).
    printer_preset_source: Mapped[str] = mapped_column(String(20), nullable=False)
    printer_preset_id: Mapped[str] = mapped_column(String(200), nullable=False)
    process_preset_source: Mapped[str] = mapped_column(String(20), nullable=False)
    process_preset_id: Mapped[str] = mapped_column(String(200), nullable=False)
    filament_presets_json: Mapped[str] = mapped_column(Text, nullable=False)  # JSON list[{"source","id"}]
    bed_type: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
