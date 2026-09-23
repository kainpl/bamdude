"""File-backed preview transport. Owned I/O is joined even after cancellation."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import threading
import zipfile
from pathlib import Path

from backend.app.services.preview_protocol import (
    CHUNK_BYTES,
    OBJECT_BYTES,
    PNG_BYTES,
    PNG_PIXELS,
    ZIP_BYTES,
    Artifact,
    PreviewError,
    remaining,
)


async def owned(awaitable):
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A canceled executor Future does not stop the syscall. Do not release
        # its descriptor, staging or admission slot while it still owns them.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


async def disk(function, *args):
    return await owned(asyncio.to_thread(function, *args))


def signature(path: Path) -> tuple:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise PreviewError("render_failed")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def snapshot(source: Path | bytes, destination: Path, deadline: int) -> None:
    before = signature(source) if isinstance(source, Path) else None
    size = before[2] if before else len(source)
    if size > OBJECT_BYTES:
        raise PreviewError("resource_limit")
    with destination.open("xb") as target:
        if isinstance(source, bytes):
            for offset in range(0, len(source), CHUNK_BYTES):
                remaining(deadline)
                target.write(memoryview(source)[offset : offset + CHUNK_BYTES])
        else:
            with source.open("rb") as stream:
                count = 0
                while block := stream.read(CHUNK_BYTES):
                    remaining(deadline)
                    count += len(block)
                    if count > OBJECT_BYTES:
                        raise PreviewError("resource_limit")
                    target.write(block)
            if signature(source) != before:
                raise PreviewError("render_failed")


def describe(path: Path, attempt: str, role: str, kind: str, deadline: int) -> Artifact:
    checksum = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while block := stream.read(CHUNK_BYTES):
            remaining(deadline)
            size += len(block)
            if size > OBJECT_BYTES:
                raise PreviewError("resource_limit")
            checksum.update(block)
    return Artifact.parse(
        {"role": role, "key": f"{attempt}_{role}", "kind": kind, "size": size, "digest": checksum.hexdigest()}, attempt
    )


def validate(path: Path, kind: str, deadline: int) -> None:
    remaining(deadline)
    if path.stat().st_size > OBJECT_BYTES:
        raise PreviewError("resource_limit")
    if kind == "png":
        from PIL import Image

        if path.stat().st_size > PNG_BYTES:
            raise PreviewError("resource_limit")
        with Image.open(path) as img:
            if img.format != "PNG" or img.width * img.height > PNG_PIXELS:
                raise PreviewError("resource_limit")
            img.verify()
    elif kind == "3mf":
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 10000 or len({x.filename for x in entries}) != len(entries):
                raise PreviewError("resource_limit")
            if sum(x.file_size for x in entries) > ZIP_BYTES:
                raise PreviewError("resource_limit")
            total = 0
            for entry in entries:
                with archive.open(entry) as stream:
                    while block := stream.read(CHUNK_BYTES):
                        remaining(deadline)
                        total += len(block)
                        if total > ZIP_BYTES:
                            raise PreviewError("resource_limit")


class _Reader:
    def __init__(self, stream, deadline: int):
        self.stream, self.deadline = stream, deadline
        self.count = 0
        self.lock = threading.Lock()

    def readinto(self, buffer):
        with self.lock:
            remaining(self.deadline)
            count = self.stream.readinto(buffer)
            self.count += count
            if self.count > OBJECT_BYTES:
                raise PreviewError("resource_limit")
            return count

    def close(self):
        with self.lock:
            self.stream.close()


class _Writer:
    def __init__(self, stream, ref: Artifact, deadline: int):
        self.stream, self.ref, self.deadline = stream, ref, deadline
        self.count = 0
        self.checksum = hashlib.sha256()
        self.lock = threading.Lock()

    def write(self, block):
        with self.lock:
            remaining(self.deadline)
            self.count += len(block)
            if self.count > self.ref.size or len(block) > CHUNK_BYTES:
                raise PreviewError("resource_limit")
            self.checksum.update(block)
            return self.stream.write(block)

    def close(self):
        with self.lock:
            self.stream.close()


async def put(store, path: Path, ref: Artifact, deadline: int) -> None:
    from nats.js.api import ObjectMeta, ObjectMetaOptions
    from nats.js.errors import ObjectNotFoundError

    try:
        await store.get_info(ref.key)
    except ObjectNotFoundError:
        pass
    else:
        raise PreviewError("protocol_error")  # immutable, never overwrite
    stream = await disk(path.open, "rb")
    reader = _Reader(stream, deadline)
    try:
        async with asyncio.timeout(remaining(deadline)):
            await store.put(ref.key, reader, meta=ObjectMeta(options=ObjectMetaOptions(max_chunk_size=CHUNK_BYTES)))
    finally:
        await disk(reader.close)
    remaining(deadline)


async def get(store, ref: Artifact, destination: Path, deadline: int) -> None:
    info = await store.get_info(ref.key)
    # nats-py's link branch ignores writeinto; disallow it BEFORE get().
    if info.is_link() or info.size != ref.size:
        raise PreviewError("protocol_error")
    partial = destination.with_suffix(destination.suffix + ".part")
    stream = await disk(partial.open, "xb")
    sink = _Writer(stream, ref, deadline)
    try:
        async with asyncio.timeout(remaining(deadline)):
            await store.get(ref.key, writeinto=sink)
    finally:
        await disk(sink.close)
    if sink.count != ref.size or sink.checksum.hexdigest() != ref.digest:
        raise PreviewError("protocol_error")
    await disk(validate, partial, ref.kind, deadline)
    await disk(os.replace, partial, destination)
    remaining(deadline)
