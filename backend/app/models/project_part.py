"""Project parts ledger: desired quantity per canonical part name (m158).

One row per (project, name_key) — the same part sliced in several layouts
feeds one counter. target_qty=0 means "not set yet". Rows survive file
unlinking: targets belong to the project, not the file.
"""

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class ProjectPart(Base):
    __tablename__ = "project_parts"
    __table_args__ = (UniqueConstraint("project_id", "name_key", name="uq_project_parts_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(512))
    name_key: Mapped[str] = mapped_column(String(512))
    target_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
