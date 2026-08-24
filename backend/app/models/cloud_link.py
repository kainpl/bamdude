"""Cloud Link — this instance's link to the BamDude portal.

Three tables, and the split between them is the point.

``cloud_link`` is one row: the pairing itself. ``cloud_link_printers`` is the
allowlist of machines that pairing may speak about, kept separate so that
turning the link on decides nothing about *what* leaves the LAN — a farm can be
paired and expose one printer. ``cloud_link_audit`` is the record of what
actually crossed, which is the only way an operator can answer "what did the
portal see" after the fact; folding it into the link row would keep a count and
lose the history.

Nothing here is a cache of portal state. Every column is a local decision or a
local observation, so a portal that goes away leaves this instance intact.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base

#: The link is a singleton — there is one instance and it pairs with one portal.
CLOUD_LINK_ROW_ID = 1

#: Where pairing points when nobody has said otherwise.
DEFAULT_PORTAL_URL = "https://cloud.bamdude.top"


class CloudLink(Base):
    """The pairing: whether it is on, where it points, and how it last went.

    One row, ``id = CLOUD_LINK_ROW_ID``. The primary key is deliberately NOT
    autoincrementing: a singleton that hands out keys invites a second row, and
    a second row here is two answers to "is this farm reachable from outside".
    Writers name the id; there is only one to name.

    ``enabled`` defaults FALSE and is never backfilled. Upgrading BamDude must
    not connect a farm to anything — that is a decision a person makes on the
    settings page, which is also why ``cloud_link:manage`` is denied to API
    keys (see ``core/auth.py``).

    ``revoked`` is distinct from ``not enabled``: disabled is this side
    stopping, revoked is the portal having thrown the credential away. They
    need different repairs — one flips a switch, the other must pair again —
    and a single flag would send half the users down the wrong one.

    ``last_error`` holds whatever the far end said, verbatim and unbounded.
    Truncating a transport error is silent on SQLite and a 500 on PostgreSQL,
    and the text is the whole value of the field.
    """

    __tablename__ = "cloud_link"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    portal_url: Mapped[str] = mapped_column(
        String(500), nullable=False, default=DEFAULT_PORTAL_URL, server_default=DEFAULT_PORTAL_URL
    )
    #: Issued by the portal at pairing; NULL until then.
    instance_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Fernet ciphertext, never the secret itself. TEXT because the token is
    #: long and grows with the key format — a VARCHAR here would be a size
    #: guess that only fails once somebody has already paired.
    instance_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")


class CloudLinkPrinter(Base):
    """A printer this instance agrees to expose. Absence means no.

    The printer id IS the row, so a machine is listed once or not at all —
    there is no second copy to disagree with the first, and no order in which
    two rows for one printer would have to be resolved.

    An allowlist rather than a flag on ``printers``: the question "may the
    portal see this" belongs to the link, not to the machine, and a farm that
    unpairs should forget its answers rather than carry them on every printer
    row forever.

    ⚠️ ``ondelete="CASCADE"`` is decorative on SQLite — this codebase never
    issues ``PRAGMA foreign_keys=ON`` — so ``CloudLinkPrinter`` is also listed
    in ``PRINTER_CASCADE_MODELS`` (``routes/printers.py``), which is what
    actually removes the row on the default database. A drift guard in
    ``test_delete_printer_dependents`` fails if it ever isn't.
    """

    __tablename__ = "cloud_link_printers"

    printer_id: Mapped[int] = mapped_column(
        ForeignKey("printers.id", ondelete="CASCADE"), primary_key=True, autoincrement=False
    )


class CloudLinkAudit(Base):
    """What crossed the link, one row per notable message.

    Append-only and never read back into behaviour: this exists so a person can
    answer "what did the portal see, and when", which no counter or last-error
    field can. ``ok`` defaults TRUE because the overwhelmingly common row is a
    message that went through — a failure is the thing worth spelling out.

    ``direction`` is "up" (we told the portal) or "down" (the portal asked us).
    Kept as text rather than an enum: the set is small but the value is read by
    humans in a table, and a database enum would need a migration to add the
    third direction that a later phase might want.
    """

    __tablename__ = "cloud_link_audit"
    #: The list is always "most recent first", and always bounded by time.
    __table_args__ = (Index("ix_cloud_link_audit_ts", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
