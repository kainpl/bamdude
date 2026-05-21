"""Per-(user, printer-model) saved print parameters.

When the operator submits a print (direct, queue add, or auto-queue add)
through the PrintModal, the toggles in the "Print options" + swap-macro
panels are persisted as a preference keyed by ``(user_id, printer_model)``.
The next time that operator opens the PrintModal targeting any printer
of the same model, the saved values pre-populate the toggles instead of
the global defaults.

Per-model rather than per-printer:
    A farm of identical machines (e.g. four P1S) shares one preference
    row per operator — calibrating "I always want bed_levelling on for
    my P1S workflow" once carries across all four.

Per-user rather than global:
    Different operators have different print-prep habits and different
    risk tolerances around mesh fast-check / layer inspect.

System fallback row (``user_id IS NULL``):
    A row with NULL ``user_id`` is the *system default* for a model — the
    fallback consulted by the virtual-printer queue-receive path when a
    slicer sends a file but omits the print-option flags (upstream
    Bambuddy #1235). Order of precedence in that path is
    slicer-sent value → system row for the target model → column default.
    There is no user in a slicer→VP handshake, so the per-user rows above
    cannot serve here; the system row fills that gap. At most one system
    row per model is enforced by the ``uq_print_options_pref_system_model``
    partial unique index (the composite ``(user_id, printer_model)`` unique
    treats distinct NULLs as non-conflicting, so it can't guard this on its
    own).

NOT stored here (intentionally per-job):
    AMS mapping, plate id, scheduling fields (manual_start, scheduled_time,
    auto_off_after), filament overrides — these are file-specific or
    per-submission decisions and would mislead if remembered.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class PrintOptionsPreference(Base):
    """Saved PrintModal toggles for one (user, printer-model) pair."""

    __tablename__ = "print_options_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", "printer_model", name="uq_print_options_pref_user_model"),
        # At most one system fallback row (user_id IS NULL) per model — the
        # composite unique above can't enforce this because both SQLite and
        # PostgreSQL treat NULLs as distinct in a multi-column UNIQUE.
        Index(
            "uq_print_options_pref_system_model",
            "printer_model",
            unique=True,
            sqlite_where=text("user_id IS NULL"),
            postgresql_where=text("user_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # NULL == the system fallback row for ``printer_model`` (see module
    # docstring). Non-NULL == a real operator's saved PrintModal toggles.
    user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # Free-form printer model string ("P1S", "X1C", "A1mini", etc.). Matches
    # the same casing the printer reports — we don't normalise here because
    # ``Printer.model`` itself isn't normalised consistently across the
    # codebase, and the preference write/read paths both pass through the
    # same source so they round-trip on whatever string the printer emits.
    printer_model: Mapped[str] = mapped_column(String(64), nullable=False)
    # JSON payload — see PrintOptionsPreferenceData schema for the contract.
    # Storing as a single JSON blob (instead of per-toggle columns) keeps
    # future toggle additions migration-free.
    options: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<PrintOptionsPreference id={self.id} user_id={self.user_id} printer_model={self.printer_model!r}>"
