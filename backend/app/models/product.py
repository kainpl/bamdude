"""Product (виріб) — catalog entity: composition + plate recipes + model card.

Parts belong to a product, not to a global catalog. ``qty_per_unit = 0`` means
"present on a plate but not part of the product" (the "zero = don't measure"
rule). A plate's yield is never cached: it is derived on read from
``LibraryFile.file_metadata`` through the parts' ``aliases`` (see
``services/product_composition.py``). Pivot tables are pure M2M, cascading on
both FKs — PostgreSQL enforces, SQLite paths clean up explicitly.
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base

product_files = Table(
    "product_files",
    Base.metadata,
    Column("product_id", Integer, ForeignKey("products.id", ondelete="CASCADE"), primary_key=True),
    Column("library_file_id", Integer, ForeignKey("library_files.id", ondelete="CASCADE"), primary_key=True),
)

product_folders = Table(
    "product_folders",
    Base.metadata,
    Column("product_id", Integer, ForeignKey("products.id", ondelete="CASCADE"), primary_key=True),
    Column("library_folder_id", Integer, ForeignKey("library_folders.id", ondelete="CASCADE"), primary_key=True),
)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)  # HTML (TipTap)
    # Model card (spec §Product card) — filled by pass 4, columns exist from day one.
    designer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    license: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    design_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Explicit cover; NULL = first ``pictures`` attachment (spec decision 6).
    cover_image_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # [{"category": "pictures|bom_docs|assembly|other", "filename", "original_name",
    #   "size", "sort_order", "source": "3mf|manual", "uploaded_at"}]
    attachments: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Catalog flag: an inactive product is not offered for new order lines.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    parts: Mapped[list["ProductPart"]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="ProductPart.sort_order"
    )
    plates: Mapped[list["ProductPlate"]] = relationship(back_populates="product", cascade="all, delete-orphan")
    library_files: Mapped[list["LibraryFile"]] = relationship(
        "LibraryFile", secondary="product_files", back_populates="products"
    )
    library_folders: Mapped[list["LibraryFolder"]] = relationship(
        "LibraryFolder", secondary="product_folders", back_populates="products"
    )
    lines: Mapped[list["ProjectLine"]] = relationship(back_populates="product")


class ProductPart(Base):
    __tablename__ = "product_parts"
    __table_args__ = (UniqueConstraint("product_id", "name_key", name="uq_product_parts_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="printed")  # printed | purchased
    name: Mapped[str] = mapped_column(String(512))
    # printed: canonical lower-case key (services/part_names.name_key);
    # purchased: "purchased:<lower name>" so it can never collide with a printed key.
    name_key: Mapped[str] = mapped_column(String(512))
    qty_per_unit: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # printed only: every object name_key that IS this part. Unique across a
    # product's parts — the service enforces it, the DB covers only name_key.
    aliases: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Still the seeded default; cleared by any operator edit.
    auto: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    # purchased only
    unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    sourcing_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    product: Mapped["Product"] = relationship(back_populates="parts")


class ProductPlate(Base):
    """One plate of one linked file. ``plate_index`` 0 = the whole file
    (single-plate 3MF, raw gcode); 1..N = that plate of a multi-plate 3MF —
    the same convention archives and the old plan used."""

    __tablename__ = "product_plates"
    __table_args__ = (
        UniqueConstraint("product_id", "library_file_id", "plate_index", name="uq_product_plates_file_plate"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    library_file_id: Mapped[int] = mapped_column(ForeignKey("library_files.id", ondelete="CASCADE"), index=True)
    plate_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    product: Mapped["Product"] = relationship(back_populates="plates")
    library_file: Mapped["LibraryFile"] = relationship()


from backend.app.models.library import LibraryFile, LibraryFolder  # noqa: E402
from backend.app.models.project_line import ProjectLine  # noqa: E402
