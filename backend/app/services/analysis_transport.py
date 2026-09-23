"""Bounded Object Store exchange for parser output; never copy input 3MF."""

from __future__ import annotations

import asyncio
import hashlib
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from backend.app.services.preview_artifacts import _Writer, disk
from backend.app.services.preview_protocol import CHUNK_BYTES, PreviewError, remaining

ARTIFACT_BYTES = 32 * 1024 * 1024
BUCKET_BYTES = 256 * 1024 * 1024
STAGING_BYTES = 512 * 1024 * 1024
TTL_SECONDS = 900
_HEX = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class AnalysisArtifact:
    key: str
    size: int
    digest: str

    def wire(self) -> dict:
        return asdict(self)

    @classmethod
    def parse(cls, data: dict, attempt_id: str):
        if not isinstance(data, dict) or set(data) != {"key", "size", "digest"}:
            raise PreviewError("protocol_error")
        if not _HEX.fullmatch(attempt_id) or data["key"] != f"{attempt_id}_analysis":
            raise PreviewError("protocol_error")
        if type(data["size"]) is not int or not 0 < data["size"] <= ARTIFACT_BYTES:
            raise PreviewError("protocol_error")
        if not isinstance(data["digest"], str) or not _SHA.fullmatch(data["digest"]):
            raise PreviewError("protocol_error")
        return cls(**data)


class _AnalysisReader:
    def __init__(self, stream, deadline: int):
        self.stream = stream
        self.deadline = deadline
        self.count = 0
        self.digest = hashlib.sha256()
        self.lock = threading.Lock()

    def readinto(self, buffer):
        with self.lock:
            remaining(self.deadline)
            count = self.stream.readinto(buffer)
            self.count += count
            if self.count > ARTIFACT_BYTES:
                raise PreviewError("resource_limit")
            self.digest.update(memoryview(buffer)[:count])
            return count

    def close(self):
        with self.lock:
            self.stream.close()


def describe(path: Path, attempt_id: str, deadline: int) -> AnalysisArtifact:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while block := stream.read(CHUNK_BYTES):
            remaining(deadline)
            size += len(block)
            if size > ARTIFACT_BYTES:
                raise PreviewError("resource_limit")
            digest.update(block)
    return AnalysisArtifact.parse(
        {"key": f"{attempt_id}_analysis", "size": size, "digest": digest.hexdigest()}, attempt_id
    )


async def put(store, path: Path, ref: AnalysisArtifact, deadline: int) -> None:
    from nats.js.api import ObjectMeta, ObjectMetaOptions
    from nats.js.errors import ObjectNotFoundError

    try:
        await store.get_info(ref.key)
    except ObjectNotFoundError:
        pass
    else:
        raise PreviewError("protocol_error")
    stream = await disk(path.open, "rb")
    reader = _AnalysisReader(stream, deadline)
    try:
        async with asyncio.timeout(remaining(deadline)):
            await store.put(ref.key, reader, meta=ObjectMeta(options=ObjectMetaOptions(max_chunk_size=CHUNK_BYTES)))
    finally:
        await disk(reader.close)
    if reader.count != ref.size or reader.digest.hexdigest() != ref.digest:
        raise PreviewError("protocol_error")


async def get(store, ref: AnalysisArtifact, path: Path, deadline: int) -> None:
    info = await store.get_info(ref.key)
    if info.is_link() or info.size != ref.size:
        raise PreviewError("protocol_error")
    stream = await disk(path.open, "xb")
    sink = _Writer(stream, ref, deadline)
    try:
        async with asyncio.timeout(remaining(deadline)):
            await store.get(ref.key, writeinto=sink)
    finally:
        await disk(sink.close)
    if sink.count != ref.size or sink.checksum.hexdigest() != ref.digest:
        raise PreviewError("protocol_error")
