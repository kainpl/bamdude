"""Display state of a queue, independent of the printer's physical state."""

from typing import Literal

QueueState = Literal["error", "paused", "printing", "idle", "unknown"]


def effective_queue_state(status: str, is_paused: bool) -> QueueState:
    if status == "error":
        return "error"
    if is_paused or status == "paused":
        return "paused"
    if status in ("printing", "idle"):
        return status
    return "unknown"
