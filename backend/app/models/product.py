"""Product (виріб) — catalog entity: composition + plate recipes + model card.

Parts belong to a product, not to a global catalog. ``qty_per_unit = 0`` means
"present on a plate but not part of the product" (the "zero = don't measure"
rule). A plate's yield is never cached: it is derived on read from
``LibraryFile.file_metadata`` through the parts' ``aliases`` (see
``services/product_composition.py``). Pivot tables are pure M2M, cascading on
both FKs — PostgreSQL enforces, SQLite paths clean up explicitly.
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
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


class ProductOrigin(StrEnum):
    """Who made this product exist (spec Decision 2).

    ``catalog`` — a person; everything that existed before m166. ``adhoc_job`` —
    the library wizard, over N files. ``adhoc_plate`` — the print dialog, for
    ONE plate of ONE file; ``origin_file_id`` + ``origin_plate_index`` are its
    identity and the partial unique index below keeps it unique. Promotion to
    ``catalog`` is one-way and clears nothing.
    """

    CATALOG = "catalog"
    ADHOC_JOB = "adhoc_job"
    ADHOC_PLATE = "adhoc_plate"


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        Index(
            "ux_products_origin_plate",
            "origin_file_id",
            "origin_plate_index",
            unique=True,
            sqlite_where=text("origin = 'adhoc_plate'"),
            postgresql_where=text("origin = 'adhoc_plate'"),
        ),
        # Named as m191 names it, so create_all and a migrated database agree.
        Index("ix_products_sku_key", "sku_key", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)  # HTML (TipTap)
    # Model card (spec §Product card): filled from a linked 3MF by
    # ``product_card.fill_from_file``, or typed by hand — never overwritten.
    designer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    license: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    design_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Explicit cover; NULL = first ``pictures`` attachment (spec decision 6).
    cover_image_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # [{"category": "pictures|bom_docs|assembly|other", "filename", "original_name",
    #   "size", "sort_order", "source": "3mf|manual|import", "source_file_id", "uploaded_at"}]
    attachments: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Catalog flag: an inactive product is not offered for new order lines.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    # Spec 2026-09-06 (orders from files), Decision 2. Never ``is_active``:
    # "deactivated" is an operator's statement about a catalogue product.
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="catalog", server_default="catalog")
    # Identity of an ``adhoc_plate`` product. No DB cascade — SQLite ignores it
    # anyway; ``product_sync.purge_file_product_links`` nulls it in code.
    origin_file_id: Mapped[int | None] = mapped_column(ForeignKey("library_files.id"), nullable=True)
    origin_plate_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # spec workshop-product-catalog, rules 1 and 14–15: the operator's catalog
    # fields. ``sku_key`` is ``sku_key(sku)`` — unique when set, computed in Python;
    # wide enough for casefold(), which may lengthen a 64-character SKU threefold.
    sku: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sku_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", server_default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    category: Mapped["ProductCategory | None"] = relationship(lazy="joined")
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
    # spec workshop-product-variants, rule 1: a choice an order makes once per unit.
    variant_groups: Mapped[list["ProductVariantGroup"]] = relationship(
        cascade="all, delete-orphan", order_by="(ProductVariantGroup.position, ProductVariantGroup.id)"
    )


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
    # spec workshop-order-issue-followups, rule 34: «не рахувати» — not a part of this
    # product at all (a test cube, an object of another plate of the file): no shelf, no
    # need, no kit, and no line may want it. Only with ``qty_per_unit = 0``. A zero WITHOUT
    # it is a part out of the kit, and it has a shelf (``line_composition.has_shelf``).
    ignored: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    # purchased only
    unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    sourcing_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # spec workshop-product-variants, rule 3: in the kit only when a line chose
    # this option; NULL = in every configuration.
    variant_option_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_variant_options.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # spec part-thumbnails §8.6: which picture the part shows. Only the chosen source's data is
    # kept, and services/part_images.py is the only writer. No FK on image_file_id: a pin whose
    # file went explains itself (``file_unlinked``) instead of silently becoming "auto" (plan E4, D9).
    image_source: Mapped[str] = mapped_column(String(16), nullable=False, default="auto", server_default="auto")
    image_file_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_plate_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_identify_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # a slicer id is u32
    image_photo: Mapped[str | None] = mapped_column(String(64), nullable=True)

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


PRODUCT_STATUSES = ("draft", "ready")


def sku_key(sku: str) -> str:
    """``Product.sku_key`` — Python's Unicode-aware fold, whatever the database folds."""
    return sku.strip().casefold()


FACET_KINDS = ("material", "color", "model")


class ProductFacet(Base):
    """What a product's plates are made of and sliced for — STORED, one writer:
    ``services/product_facets.py`` (spec workshop-product-catalog, rules 3 and 5).

    A row per FILE: the catalog skips a trashed file's rows when it reads them
    (owner, 2026-09-27), so trashing and restoring change nothing here."""

    __tablename__ = "product_facets"
    __table_args__ = (Index("ix_product_facets_kind_value", "kind", "value"),)

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), primary_key=True)
    library_file_id: Mapped[int] = mapped_column(ForeignKey("library_files.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    value: Mapped[str] = mapped_column(String(64), primary_key=True)


from backend.app.models.library import LibraryFile, LibraryFolder  # noqa: E402
from backend.app.models.product_category import ProductCategory  # noqa: E402
from backend.app.models.product_variant import ProductVariantGroup  # noqa: E402
from backend.app.models.project_line import ProjectLine  # noqa: E402
