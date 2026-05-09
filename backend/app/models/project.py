from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class Project(Base):
    """Project to group related prints (e.g., 'Voron Build' with multiple parts)."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    color: Mapped[str | None] = mapped_column(String(20), nullable=True)  # Hex color for UI
    status: Mapped[str] = mapped_column(String(20), default="active")  # active, completed, archived
    target_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # Optional target number of prints (plates)
    target_parts_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # Optional target number of parts/objects

    # Phase 2: Rich text notes (HTML from WYSIWYG editor)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Phase 3: File attachments stored as JSON array
    # Format: [{"filename": "x.stl", "original_name": "part.stl", "size": 1234, "uploaded_at": "..."}]
    attachments: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Phase 4: Tags (comma-separated)
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Phase 5: Due dates and priority
    due_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    priority: Mapped[str] = mapped_column(String(20), default="normal")  # low, normal, high, urgent

    # Phase 6: Budget tracking
    budget: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Phase 8: Templates
    is_template: Mapped[bool] = mapped_column(Boolean, default=False)
    template_source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Phase 10: Sub-projects (hierarchical)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)

    # B.2 (#1155) — external link rendered as a clickable icon next to the project
    # name (Bambuddy / MakerWorld / GitHub / Notion etc). Validated http(s) only
    # in the schema layer so a `javascript:`-style XSS payload can't reach the DOM.
    url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # B.2 (#1155) — filename of the cover photo inside the project's attachments
    # dir; serves as the card's hero image. Tracked separately from the
    # ``attachments`` JSON list so swapping/deleting one doesn't disturb the other.
    cover_image_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    archives: Mapped[list["PrintArchive"]] = relationship(back_populates="project")
    queue_items: Mapped[list["PrintQueueItem"]] = relationship(back_populates="project")
    children: Mapped[list["Project"]] = relationship(
        "Project",
        back_populates="parent",
        foreign_keys="Project.parent_id",
    )
    parent: Mapped["Project | None"] = relationship(
        "Project",
        back_populates="children",
        remote_side="Project.id",
        foreign_keys="Project.parent_id",
    )
    bom_items: Mapped[list["ProjectBOMItem"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    # m044: many-to-many. A project can carry any number of library
    # files / folders. Pivot tables: library_file_projects /
    # library_folder_projects, both ON DELETE CASCADE.
    library_files: Mapped[list["LibraryFile"]] = relationship(
        "LibraryFile",
        secondary="library_file_projects",
        back_populates="projects",
    )
    library_folders: Mapped[list["LibraryFolder"]] = relationship(
        "LibraryFolder",
        secondary="library_folder_projects",
        back_populates="projects",
    )


# Pivot tables for the M2M ``library_files`` / ``library_folders`` relationships
# above. Imported for side-effect: registers ``library_file_projects`` /
# ``library_folder_projects`` Table objects in ``Base.metadata`` so the
# ``secondary="..."`` string lookups resolve when SQLAlchemy configures
# mappers — otherwise tests that import only Project (transitively) hit
# ``NameError: name 'library_file_projects' is not defined`` at mapper init.
from backend.app.models import library_project_links  # noqa: E402, F401
from backend.app.models.archive import PrintArchive  # noqa: E402
from backend.app.models.library import LibraryFile, LibraryFolder  # noqa: E402
from backend.app.models.print_queue import PrintQueueItem  # noqa: E402
from backend.app.models.project_bom import ProjectBOMItem  # noqa: E402
