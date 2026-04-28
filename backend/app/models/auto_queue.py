"""Auto-queue layer above per-printer print_queue.

Items in this table are *pre-dispatch*: they describe the print's
requirements (target_model, location, filament types) without being
bound to a specific printer. The AutoQueueScheduler periodically scans
pending rows, finds an eligible idle printer, and *copies* the item
into that printer's print_queue. The auto row is then marked
``status='assigned'`` with a back-reference to the per-printer item.

Why a separate table (vs nullable queue_id on print_queue):
- Existing per-printer dispatch flow stays untouched (no nullable FK
  surprises in 20+ code paths that read ``item.queue.printer_id``).
- Routing fields (target_model, required_filament_types,
  filament_overrides, force_color_match) only matter pre-dispatch —
  they don't need to live on every print_queue row.
- Clean conceptual separation: this is a *router*, the print_queue
  is the *executor*.

See ``temp/auto-queue-adaptation-variants.md`` §12 for the full design.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class AutoQueueItem(Base):
    """Pre-dispatch queue item — auto-distributed to any eligible printer."""

    __tablename__ = "auto_queue_items"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Source file (mirrors print_queue: archive_id XOR library_file_id)
    archive_id: Mapped[int | None] = mapped_column(ForeignKey("print_archives.id", ondelete="CASCADE"), nullable=True)
    library_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_files.id", ondelete="SET NULL"), nullable=True
    )
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)

    # Routing target (upstream-style)
    # target_model: normalized printer model code, e.g. "X1C", "P1S", "K1C", "A1MINI"
    target_model: Mapped[str | None] = mapped_column(String(50), nullable=True)
    target_location: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # JSON array of required filament types extracted from 3MF, user-overridable
    # Example: '["PLA","PETG"]'
    required_filament_types: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON array of filament overrides, same format as upstream:
    # [{"slot_id":1,"type":"PLA","color":"#FF0000","force_color_match":true}, ...]
    filament_overrides: Mapped[str | None] = mapped_column(Text, nullable=True)
    force_color_match: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Multi-plate: one plate = one row (plate_id is 1-indexed)
    plate_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Print options — copied verbatim into print_queue on assignment
    bed_levelling: Mapped[bool] = mapped_column(Boolean, default=True)
    flow_cali: Mapped[bool] = mapped_column(Boolean, default=True)
    layer_inspect: Mapped[bool] = mapped_column(Boolean, default=False)
    timelapse: Mapped[bool] = mapped_column(Boolean, default=False)
    use_ams: Mapped[bool] = mapped_column(Boolean, default=True)
    mesh_mode_fast_check: Mapped[bool] = mapped_column(Boolean, default=True)
    execute_swap_macros: Mapped[bool] = mapped_column(Boolean, default=True)
    swap_macro_events: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Scheduling
    position: Mapped[int] = mapped_column(Integer, default=0)
    scheduled_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    manual_start: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_off_after: Mapped[bool] = mapped_column(Boolean, default=False)
    require_previous_success: Mapped[bool] = mapped_column(Boolean, default=False)

    # Lifecycle
    # status: pending | assigned | cancelled
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    waiting_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    assigned_to_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("print_queue.id", ondelete="SET NULL"), nullable=True
    )
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # SJF + been_jumped (sticky starvation guard, never reset)
    print_time_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    been_jumped: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Batch grouping — UUID v4 shared across N copies; mirrors print_queue.batch_id
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    # Tracking
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Relationships
    archive: Mapped["PrintArchive | None"] = relationship()
    library_file: Mapped["LibraryFile | None"] = relationship()
    project: Mapped["Project | None"] = relationship()
    created_by: Mapped["User | None"] = relationship()
    assigned_to: Mapped["PrintQueueItem | None"] = relationship(foreign_keys=[assigned_to_item_id])


from backend.app.models.archive import PrintArchive  # noqa: E402
from backend.app.models.library import LibraryFile  # noqa: E402
from backend.app.models.print_queue import PrintQueueItem  # noqa: E402
from backend.app.models.project import Project  # noqa: E402
from backend.app.models.user import User  # noqa: E402
