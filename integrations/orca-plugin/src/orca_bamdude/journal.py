"""The plugin's durable record of what it sent (spec r2 §5.3, §8.4).

One JSON file per operation, written atomically (temp file, fsync, replace), inside a folder that belongs to one
namespace -- this server's connector id, its storage epoch and the API key's identity. Another server, a restored
server or another key never reads this history as its own.

The operation id is created and written BEFORE the first request: a lost answer is reconciled by asking for that
same id, never by sending again under a new one. A record's ``phase`` says how far the plugin got; ``unknown``
means a request went out and its answer did not come back.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass

PHASES = ("prepared", "uploading", "uploaded", "submitting", "accepted", "unknown", "finished", "upload_only")


def _folder_name(namespace: tuple[str, str, str]) -> str:
    joined = "\n".join(namespace).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()[:32]


def _write_atomic(path: str, payload: dict) -> None:
    partial = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(partial, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, path)


@dataclass
class Record:
    operation_id: str
    phase: str
    created_at: float
    filename: str
    source_sha256: str
    library_file_id: int | None = None
    body: dict | None = None
    result: dict | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class Journal:
    def __init__(self, root: str, namespace: tuple[str, str, str]):
        self.folder = os.path.join(root, "journal", _folder_name(namespace))
        os.makedirs(self.folder, exist_ok=True)

    def _path(self, operation_id: str) -> str:
        str(uuid.UUID(operation_id))  # a path component only ever comes from a UUID
        return os.path.join(self.folder, f"{operation_id}.json")

    def create(self, filename: str, source_sha256: str) -> Record:
        record = Record(
            operation_id=str(uuid.uuid4()),
            phase="prepared",
            created_at=time.time(),
            filename=filename,
            source_sha256=source_sha256,
        )
        self.save(record)
        return record

    def save(self, record: Record) -> None:
        if record.phase not in PHASES:
            raise ValueError(record.phase)
        _write_atomic(self._path(record.operation_id), record.as_dict())

    def load(self, operation_id: str) -> Record | None:
        try:
            with open(self._path(operation_id), encoding="utf-8") as handle:
                return Record(**json.load(handle))
        except FileNotFoundError:
            return None

    def records(self) -> list[Record]:
        found = []
        for name in sorted(os.listdir(self.folder)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.folder, name), encoding="utf-8") as handle:
                    found.append(Record(**json.load(handle)))
            except (OSError, ValueError, TypeError):
                continue  # a damaged record is skipped, never rewritten
        return sorted(found, key=lambda r: r.created_at, reverse=True)

    def pending(self) -> list[Record]:
        """What needs reconciliation: a request went out and the final answer is not recorded."""
        return [r for r in self.records() if r.phase in ("uploading", "submitting", "unknown", "accepted")]
