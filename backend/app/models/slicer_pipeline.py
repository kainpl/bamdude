"""Slicer Pipeline — a saved, reusable bundle of the four SliceModal slot picks
(printer / process / filament(s) / bed) plus a dispatch target and fanout strategy.

Ported from upstream Bambuddy #1425 (Slicer Pipelines). Column names mirror upstream
for frontend/schema parity; the enqueue side adapts to BamDude's two-tier queue
(see ``pipeline_run.py`` + ``routes/pipeline_runs.py``). A "Pipeline Run" slices the
source once with these presets and enqueues N copies onto the target.

Soft-deleted (``is_deleted``) rather than hard-deleted so run history can still resolve
a pipeline's metadata after the operator "deletes" it.
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

    # Dispatch target — either a specific printer or a whole printer-model class.
    target_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="printer_class")
    target_printer_id: Mapped[int | None] = mapped_column(ForeignKey("printers.id", ondelete="SET NULL"), nullable=True)
    target_model_class: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Fanout strategy for a class target: max_parallel / fill_one_first / round_robin.
    # NOTE (BamDude adaptation): for a class target we enqueue AutoQueueItems and let the
    # AutoQueue distributor balance — max_parallel maps to that native behaviour. The
    # strategy is honoured on the pinned-printer paths (fill_one_first / round_robin).
    fanout_strategy: Mapped[str] = mapped_column(String(20), nullable=False, default="max_parallel")

    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
