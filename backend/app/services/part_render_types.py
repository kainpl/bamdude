"""Types shared by the part-render runtime, the writer and the scheduler (plan E3, task 18)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Mode = Literal["render", "fallback"]


class RuntimeUnavailable(RuntimeError):
    """Nothing was attempted: the queue waits and counts nothing (spec §5.3, §13: a fault of the runtime)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SourceRef:
    library_file_id: int
    path: str  # absolute
    root: str  # settings.library_dir, or the external folder's own path
    kind: str  # "3mf" | "gcode"
    size: int


@dataclass(frozen=True)
class RenderTask:
    render_id: int
    file_sha256: str
    plate_index: int
    renderer_version: int
    phase: str  # "render" | "fallback"
    reason: str | None  # a pending row in "fallback": why it is there
    attempts: int
    priority: int
    has_result: bool
    source: SourceRef

    def wire(self) -> dict:
        return {
            "path": self.source.path,
            "root": self.source.root,
            "kind": self.source.kind,
            "sha256": self.file_sha256,
            "size": self.source.size,
            "plate_index": self.plate_index,
        }


@dataclass
class AttemptResult:
    outcome: str  # "done" | "timeout" | "memory_limit" | "crashed" | "invalid_output" | "canceled"
    attempt_id: str
    elapsed_ms: int
    result: dict | None = None  # the child's result.json when outcome == "done"
    files: Path | None = None  # unpacked, verified files in main's staging when the result is ok
    runtime_version: str | None = None
