from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, Select, String, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class PrintArchive(Base):
    __tablename__ = "print_archives"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int | None] = mapped_column(ForeignKey("printers.id"), nullable=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    # The order line this print was made for. Set by the dispatcher when the job
    # carried one; NULL prints are attributed on read (services/order_metrics).
    project_line_id: Mapped[int | None] = mapped_column(
        ForeignKey("project_lines.id", ondelete="SET NULL"), nullable=True
    )
    # Link back to the library_files row this archive was dispatched from.
    # Populated at dispatch time (see background_dispatch.py) so library
    # usage stats (print_count, last_printed_at) can be driven off the
    # archive history instead of only live-tracked at queue completion.
    # NULL for external/manual prints or archives created before m014.
    library_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_files.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Queue this archive belongs to (1:1 with a printer, may match
    # printer_id when PrinterQueue.id == printer.id numerically, but not
    # guaranteed). Populated at dispatch time (see background_dispatch.py)
    # so historical queue counters can be recomputed from the archive
    # table itself — print_queue only keeps live pending/skipped items
    # post-m019. NULL for very old archives created before this field.
    queue_id: Mapped[int | None] = mapped_column(
        ForeignKey("printer_queues.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Same batch grouping the queue item carried. Preserved here so we
    # can still ask "how many of batch <uuid> completed?" after the
    # queue rows are cleaned up.
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # Free-form diagnostic text from dispatcher / scheduler when the
    # print failed. Short causes still live in ``failure_reason``;
    # this is the verbose twin that operators see on hover.
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # File info
    filename: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(String(500))
    file_size: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(String(64))  # SHA256 of the bytes stored in the archive dir
    # Chain-of-custody for BamDude-patched prints: hash of the UNPATCHED source
    # (library file or prior archive). NULL for external prints. Dedup queries
    # use COALESCE(source_content_hash, content_hash) so patched variants
    # collapse against their original. JSON array of applied patch identifiers
    # lives alongside for future reprint-reapply semantics.
    source_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    applied_patches: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Printer-assigned subtask identifier observed in MQTT push_status. Advisory
    # match key: on_print_start consults it as a fast pre-check before falling
    # back to our primary name + content_hash matching (#972).
    subtask_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    thumbnail_path: Mapped[str | None] = mapped_column(String(500))
    timelapse_path: Mapped[str | None] = mapped_column(String(500))
    # True when BamDude forced timelapse recording on for this print so the
    # finish-photo extractor (#1397) could pull the post-park-pre-drop frame.
    # The cleanup path uses this to know the timelapse should be deleted both
    # locally and on the printer's SD after extraction — the user didn't opt
    # in to a timelapse recording.
    bamdude_forced_timelapse: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    source_3mf_path: Mapped[str | None] = mapped_column(String(500))  # Original project 3MF from slicer
    f3d_path: Mapped[str | None] = mapped_column(String(500))  # Fusion 360 design file

    # Print details from 3MF / printer
    print_name: Mapped[str | None] = mapped_column(String(255))
    print_time_seconds: Mapped[int | None] = mapped_column(Integer)
    filament_used_grams: Mapped[float | None] = mapped_column(Float)
    filament_type: Mapped[str | None] = mapped_column(String(50))
    filament_color: Mapped[str | None] = mapped_column(String(50))
    layer_height: Mapped[float | None] = mapped_column(Float)
    total_layers: Mapped[int | None] = mapped_column(Integer)
    nozzle_diameter: Mapped[float | None] = mapped_column(Float)
    bed_temperature: Mapped[int | None] = mapped_column(Integer)
    bed_type: Mapped[str | None] = mapped_column(
        String(64)
    )  # e.g. "Cool Plate", "Textured PEI Plate" (3MF curr_bed_type)
    nozzle_temperature: Mapped[int | None] = mapped_column(Integer)

    # Printer model this file was sliced for (extracted from 3MF metadata)
    sliced_for_model: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Which plate of the source 3MF was actually sent to the printer (m038).
    # 1-indexed. NULL for: archives created before m038 where the index
    # couldn't be backfilled, single-plate prints where the distinction
    # is meaningless, and external prints whose plate origin is unknown.
    # Powers the per-plate gcode + 3D preview in ModelViewerModal so the
    # archive shows what was actually printed (not just the first plate
    # in the container).
    plate_index: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    # Print result
    status: Mapped[str] = mapped_column(String(20), default="completed")
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Real print duration + estimate accuracy, persisted (kept in sync by the
    # before_flush event in core/database.py; formula in services/archive_time_metrics).
    # Completed prints only; NULL for failed/cancelled/printing. m107.
    actual_time_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    time_accuracy: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Extended metadata (JSON blob for flexibility)
    extra_data: Mapped[dict | None] = mapped_column(JSON)

    # MakerWorld info (auto-extracted from 3MF)
    makerworld_url: Mapped[str | None] = mapped_column(String(500))
    designer: Mapped[str | None] = mapped_column(String(255))

    # User-defined external link (Printables, Thingiverse, etc.)
    external_url: Mapped[str | None] = mapped_column(String(500))

    # User additions
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    tags: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    cost: Mapped[float | None] = mapped_column(Float)
    photos: Mapped[list | None] = mapped_column(JSON)  # List of photo filenames
    failure_reason: Mapped[str | None] = mapped_column(String(100))  # For failed prints
    quantity: Mapped[int] = mapped_column(Integer, default=1)  # Number of items printed

    # How many of those items came out unusable (m122). Operator-owned: editable
    # on the archive, and raised automatically when objects are skipped — the
    # printer reports the authoritative list in the ``s_obj`` telemetry field, so
    # a skip made from the printer's own screen counts the same as one made here.
    # The automatic path only ever RAISES it (``max(current, len(s_obj))``) so it
    # cannot undo a number someone typed in. Deliberately does NOT feed
    # ``quantity`` or any of the "printed" totals — those still count what came
    # off the plate, and the split between the two is the point.
    defective_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    # Energy tracking
    energy_kwh: Mapped[float | None] = mapped_column(Float)  # Energy consumed in kWh
    energy_cost: Mapped[float | None] = mapped_column(Float)  # Cost of energy consumed
    # Plug lifetime counter captured at print start; delta at print end becomes energy_kwh.
    # Persisted so per-print tracking survives backend restarts mid-print (upstream #941).
    energy_start_kwh: Mapped[float | None] = mapped_column(Float)

    # Swap mode compatibility (processed for plate swapper)
    swap_compatible: Mapped[bool] = mapped_column(Boolean, default=False)

    # Whether per-object skipping will work for this archive's 3MF —
    # ``gcode_label_objects`` AND ``exclude_object``, both from
    # ``Metadata/project_settings.config``. Denormalised out of ``extra_data``
    # (m114) so the archive list can badge it without a JSON dig per row. Always
    # written through ``services.library_helpers.skip_objects_supported_from_metadata``
    # — never inline — so the badge cannot disagree with the live 3MF gate the
    # preview reads.
    skip_objects_supported: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Soft-delete column (m034). When non-null the archive is in the trash:
    # excluded from listings, dedup queries, project rollups, etc. Sweeper
    # hard-deletes rows whose deleted_at is older than the archive trash
    # retention window. Mirrors LibraryFile.deleted_at semantics.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    # User tracking (who uploaded/created this archive)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Filament Calibration flag (m062). True when this archive belongs to a
    # calibration print job (PA Line / Flow Rate / Tower mode). The wizard
    # tags the queue item; on_print_complete propagates the flag here so
    # operators can later filter / hide cali rows from normal archive views.
    # calibration_session_id links back to the orchestration row that
    # produced this print, if any.
    is_calibration: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    calibration_session_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # True when this print was dispatched by the AutoQueueScheduler
    # (auto_queue_items → print_queue). Set by background_dispatch at
    # archive creation from the queue item's source_auto_item_id. The
    # auto_queue_items row is deleted once its print finishes, so this
    # flag is the lasting record — it lets the auto-queue view compute
    # archive-backed completed/failed totals the same way per-printer
    # queues do.
    from_auto_queue: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    @classmethod
    def active(cls) -> "Select[tuple[PrintArchive]]":
        """Select statement that excludes trashed archives.

        Use this for any user-facing listing, dedup, project rollup, or
        history query so trashed archives don't leak into normal flows.
        Trash-specific endpoints build their own query with
        ``deleted_at.isnot(None)``.
        """
        return select(cls).where(cls.deleted_at.is_(None))

    # Relationships
    printer: Mapped["Printer | None"] = relationship(back_populates="archives")
    project: Mapped["Project | None"] = relationship(back_populates="archives")
    created_by: Mapped["User | None"] = relationship()


from backend.app.models.printer import Printer  # noqa: E402, F811
from backend.app.models.project import Project  # noqa: E402, F811
from backend.app.models.user import User  # noqa: E402, F811
