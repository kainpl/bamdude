"""A queue row's claim basis: what the scheduler judged, compared again where it decides (G03 B, A1).

The basis of a row is the canonical digest of every column of its table except the scheduler's waiting
diagnostics (and an ``updated_at`` stamp, which the queue tables' neighbours carry): the pass that judged a row
records it right after the row is loaded, and the claim and every verdict compare it with the row they lock. A
column added later is in the basis by default — an edit of it after the load is an edit of what was judged.

The same definition fences the AutoQueue transfer's preview (``queue_auto_transfer``), so the two never drift.
"""

from __future__ import annotations

from backend.app.services.print_state_recovery_preview import digest

# What the scheduler itself writes while it judges a row: never an operator's intent, never part of the basis.
EXCLUDED_COLUMNS = frozenset({"waiting_reason", "waiting_reason_code", "waiting_reason_checked_at", "updated_at"})


def basis_values(row) -> dict:
    """Every column of the row's table but the excluded ones, by column key."""
    return {
        column.key: getattr(row, column.key) for column in row.__table__.columns if column.key not in EXCLUDED_COLUMNS
    }


def row_basis(row) -> str:
    return digest(basis_values(row))
