"""A product's variant groups and their options (spec workshop-product-variants, rules 1–3).

A group is a choice an order makes once per unit — «Хвіст: прямий / кутовий».
A part is bound to a set of options, across groups (``ProductPartOption``,
spec product-part-variant-combinations): it is in a line's kit when, for every
group it has options in, the line chose one of them; an unbound part is always
in it. What a line chose is ``line_config.ProjectLineChoice``, written by
``services/line_config.py`` alone.
"""

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


def variant_key(name: str) -> str:
    """A group's or option's uniqueness key — Python's Unicode-aware fold."""
    return name.strip().casefold()


class ProductVariantGroup(Base):
    __tablename__ = "product_variant_groups"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # NULL only between the group's insert and its first option's; the routes
    # keep it pointing at one of the group's own options (the standard option
    # cannot be deleted, a deleted group takes its options). Deliberately NO
    # foreign key, as in m191: one would close a group ↔ option cycle, which
    # the portable backup's DDL keeps out of its checked set, and a fresh
    # install would carry a constraint a migrated one does not.
    default_option_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    options: Mapped[list["ProductVariantOption"]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
        order_by="(ProductVariantOption.position, ProductVariantOption.id)",
        foreign_keys="ProductVariantOption.group_id",
    )


class ProductVariantOption(Base):
    __tablename__ = "product_variant_options"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("product_variant_groups.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    group: Mapped[ProductVariantGroup] = relationship(back_populates="options", foreign_keys=[group_id])


class ProductPartOption(Base):
    """One option a part is bound to (spec product-part-variant-combinations, m225).

    ``group_id`` is the option's own group, stored so the kit rule needs no join —
    an option never changes group. ``ProductPart.bound`` is the shape the kit rule
    reads. SQLite runs no FK actions: deleting a part, option, group or product
    removes these rows in code.
    """

    __tablename__ = "product_part_options"

    part_id: Mapped[int] = mapped_column(ForeignKey("product_parts.id", ondelete="CASCADE"), primary_key=True)
    option_id: Mapped[int] = mapped_column(
        ForeignKey("product_variant_options.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[int] = mapped_column(
        ForeignKey("product_variant_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
