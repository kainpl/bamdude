"""Part thumbnails: a plate's render, both its queue row and its result header (spec §8.1), and its instances (§8.2).

Written only by ``services/part_renders.py`` (tests/unit/test_part_renders_have_one_writer.py).
"""

from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PlateRender(Base):
    __tablename__ = "plate_renders"
    __table_args__ = (
        UniqueConstraint("file_sha256", "plate_index", "renderer_version", name="uq_plate_renders_key"),
        CheckConstraint("length(file_sha256) = 64", name="ck_plate_renders_sha256"),
        CheckConstraint("status IN ('pending', 'ready', 'failed', 'unavailable')", name="ck_plate_renders_status"),
        CheckConstraint("phase IN ('render', 'fallback')", name="ck_plate_renders_phase"),
        Index("ix_plate_renders_queue", "status", "renderer_version", "next_attempt_at"),
        # render_id goes into logs, media URLs and the publication's CAS: never reissued
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    plate_index: Mapped[int] = mapped_column(Integer, nullable=False)
    renderer_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    phase: Mapped[str] = mapped_column(String(16), nullable=False, default="render")
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    result_dir: Mapped[str | None] = mapped_column(String(36), nullable=True)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    runtime_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    last_error: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    orphaned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PlateRenderObject(Base):
    __tablename__ = "plate_render_objects"
    __table_args__ = (
        UniqueConstraint("render_id", "identify_id", name="uq_plate_render_objects_instance"),
        CheckConstraint(
            "method IN ('toolpath', 'model', 'top_mask', 'missing', 'skipped')", name="ck_plate_render_objects_method"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    render_id: Mapped[int] = mapped_column(
        ForeignKey("plate_renders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    identify_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # a slicer id is u32
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tools: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
