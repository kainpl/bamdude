"""A stack entry the operator chose to hide on one printer — see ``services/hms_mute``.

One row per (printer, full 16-char ``hms[]`` code). The row exists only while
the printer keeps reporting the entry: the MQTT client drops the mute the moment
the code leaves the stack, and the manager deletes the row. Nothing is muted by
short code or by "no description" — see ``HMSErrorModal.filterKnownHMSErrors``
for what hiding by absence of text cost once.

⚠️ No migration on purpose. ``init_db`` runs ``Base.metadata.create_all`` on
every startup, which creates this table on fresh and existing installs alike
(SQLite and PostgreSQL), and there is nothing to seed. A numbered migration
would have collided with the m157–m162 block a parallel branch was carrying at
the time (2026-09-05), and under ``DEBUG=true`` the runner re-runs the highest
number on every start — which would have hijacked that branch's iteration loop.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class HMSMutedEntry(Base):
    __tablename__ = "hms_muted_entries"
    __table_args__ = (UniqueConstraint("printer_id", "full_code", name="uq_hms_muted_printer_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"), nullable=False, index=True)
    # The firmware's own key for the entry: ``attr`` + ``code`` as 16 upper-case
    # hex chars (``HMSError.full_code``). Never the lossy short form.
    full_code: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
