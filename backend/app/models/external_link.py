from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class ExternalLink(Base):
    """External links for sidebar navigation."""

    __tablename__ = "external_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    url: Mapped[str] = mapped_column(String(500))
    icon: Mapped[str] = mapped_column(String(50), default="link")
    custom_icon: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Filename of uploaded icon
    open_in_new_tab: Mapped[bool] = mapped_column(Boolean, default=False)
    # Sidebar group this link belongs to. Mirrors the frontend ``NavGroup``
    # type (operations / workshop / resources / care / system) plus the
    # dedicated ``external`` bucket — the default — for user-added links
    # that don't logically fit any built-in section. m055 backfills.
    nav_group: Mapped[str] = mapped_column(String(20), default="external")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ExternalLinkBadge(Base):
    """A count on an external sidebar link, reported by the app the link points to (m267).

    Upstream Bambuddy 29a81520d. Kept apart from the link, so a report every minute
    leaves the link's own ``updated_at`` alone. A count not reported again within
    ``routes/external_links.BADGE_FRESH_FOR`` is not shown: an app that stopped leaves
    no old number. The row belongs to the link's address — a changed address or a
    deleted link removes it in the same transaction (SQLite here runs without
    ``PRAGMA foreign_keys``, so the CASCADE below does not do it there).
    """

    __tablename__ = "external_link_badges"

    link_id: Mapped[int] = mapped_column(ForeignKey("external_links.id", ondelete="CASCADE"), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    # When the app last reported, naive UTC stamped by the writer (never a server default:
    # that is local time on a PostgreSQL whose timezone is not UTC).
    updated_at: Mapped[datetime] = mapped_column(DateTime)
