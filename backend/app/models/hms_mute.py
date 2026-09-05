"""A stack entry the operator chose to hide on one printer — see ``services/hms_mute``.

One row per (printer, full 16-char ``hms[]`` code). The row exists only while
the printer keeps reporting the entry: the MQTT client drops the mute the moment
the code leaves the stack, and the manager deletes the row. Nothing is muted by
short code or by "no description" — see ``HMSErrorModal.filterKnownHMSErrors``
for what hiding by absence of text cost once.

Created by ``create_all`` on a fresh install and by m163 on an existing one;
the two name their constraint and index the same so they describe one table.
Nothing to seed — a mute is an operator's decision about one incident.
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
