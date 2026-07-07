"""Pipeline Run + Pipeline Job — one "Run pipeline" invocation and its per-copy rows.

Ported from upstream Bambuddy #1425. A ``PipelineRun`` slices the source file once with
the pipeline's presets, then enqueues ``copies`` print jobs onto the target; each copy is a
``PipelineJob``. Column names mirror upstream for schema/frontend parity.

**BamDude adaptation (two-tier queue, see [[feedback_queue_ideology]]):** a copy is either
pinned to a specific printer (tracked via ``queue_entry_id`` → ``print_queue.id``) or
deferred to a printer-model class (tracked via ``auto_queue_item_id`` → ``auto_queue_items.id``,
which the AutoQueue distributor later assigns to a printer, spawning a ``print_queue`` row).
``auto_queue_item_id`` is the only column that has no upstream counterpart.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    pipeline_id: Mapped[int | None] = mapped_column(
        ForeignKey("slicer_pipelines.id", ondelete="SET NULL"), nullable=True
    )

    # Source — exactly one of these is set (library file XOR archive).
    source_library_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_files.id", ondelete="SET NULL"), nullable=True
    )
    source_archive_id: Mapped[int | None] = mapped_column(
        ForeignKey("print_archives.id", ondelete="SET NULL"), nullable=True
    )
    # Retry-failed spawns a child run pointing back at its parent.
    parent_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="SET NULL"), nullable=True
    )

    copies: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Snapshot status: queued / slicing / dispatching / in_progress / completed /
    # failed / cancelled / partial_failure. Live status is rolled up in the route.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")

    # In-memory slice_dispatch job id (NOT an FK — the dispatcher is process-local).
    slice_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sliced_library_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_files.id", ondelete="SET NULL"), nullable=True
    )
    eligibility_overridden: Mapped[bool] = mapped_column(default=False, server_default="0", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    jobs: Mapped[list["PipelineJob"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="PipelineJob.copy_index",
    )


class PipelineJob(Base):
    __tablename__ = "pipeline_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    pipeline_run_id: Mapped[int] = mapped_column(ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False)
    copy_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    assigned_printer_id: Mapped[int | None] = mapped_column(
        ForeignKey("printers.id", ondelete="SET NULL"), nullable=True
    )
    # Pinned-printer copy → the spawned print_queue row.
    queue_entry_id: Mapped[int | None] = mapped_column(ForeignKey("print_queue.id", ondelete="SET NULL"), nullable=True)
    # BamDude adaptation: model-class copy → the AutoQueue staging row the distributor
    # will assign to a printer (spawning a print_queue row referenced by source_auto_item_id).
    auto_queue_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("auto_queue_items.id", ondelete="SET NULL"), nullable=True
    )
    # pending / awaiting_printer / queued / printing / completed / failed / cancelled
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    run: Mapped["PipelineRun"] = relationship(back_populates="jobs")
