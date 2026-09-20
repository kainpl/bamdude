"""Record scheduling observations without changing dispatch authority."""

from datetime import datetime, timezone
from typing import Literal

WaitCode = Literal[
    "printer_offline",
    "plate_not_cleared",
    "drying",
    "scheduled",
    "manual_start",
    "stagger",
    "filament_unavailable",
    "dispatch_failed",
    "storage_full",
    "unknown",
]


def set_wait_reason(item, code: WaitCode | None, message: str | None) -> bool:
    """Return whether the row changed; repeated observations do not cause writes.

    checked_at identifies when this particular decision was established. The
    monitor still checks live facts before using it, and edits invalidate it.
    """
    if item.waiting_reason == message and getattr(item, "waiting_reason_code", None) == code:
        return False
    item.waiting_reason = message
    item.waiting_reason_code = code
    item.waiting_reason_checked_at = datetime.now(timezone.utc) if code else None
    return True
