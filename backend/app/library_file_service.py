"""Local NATS worker for bounded library walk and file preparation."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import nats

from backend.app.services.analysis_transport import describe, put
from backend.app.services.library_file_preparation import hash_file, prepare_file
from backend.app.services.preview_artifacts import disk
from backend.app.services.preview_protocol import PreviewError, decode, encode

PAGE_SIZE = 32


def _walk(root: Path, show_hidden: bool):
    def onerror(error):
        raise error

    for directory, directories, files in os.walk(root, onerror=onerror, followlinks=False):
        directories[:] = [name for name in directories if not (Path(directory) / name).is_symlink()]
        if not show_hidden:
            directories[:] = [name for name in directories if not name.startswith(".")]
            files = [name for name in files if not name.startswith(".")]
        yield {"directory": directory, "is_dir": True}
        for name in files:
            path = Path(directory) / name
            if path.is_symlink():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            yield {
                "directory": directory,
                "name": name,
                "is_dir": False,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }


class LibraryFileService:
    def __init__(self, config: dict):
        self.config = config
        self.subject = f"bamdude.library.{config['generation']}.{config['epoch']}"
        self.staging = Path(config["staging"])
        self.lock = asyncio.Lock()
        self.walks = {}
        self.seen = set()

    async def start(self):
        self.staging.mkdir(parents=True, exist_ok=True)
        self.nc = await nats.connect(
            self.config["url"], token=self.config["token"], allow_reconnect=False, connect_timeout=5
        )
        self.store = await self.nc.jetstream(timeout=5).object_store(self.config["bucket"])
        await self.nc.subscribe(self.subject, cb=self.receive)
        await self.nc.flush(timeout=5)

    async def receive(self, message):
        asyncio.create_task(self.handle(message))

    async def handle(self, message):
        try:
            command = decode(message.data)
            if command.get("generation") != self.config["generation"] or command.get("epoch") != self.config["epoch"]:
                raise PreviewError("protocol_error")
            if command.get("operation") == "ready":
                result = {"outcome": "ok", "epoch": self.config["epoch"], "monotonic_ns": time.monotonic_ns()}
            else:
                async with self.lock:
                    result = await self.run(command)
        except Exception as exc:
            result = {"outcome": "error", "reason": type(exc).__name__}
        try:
            await message.respond(encode(result))
        except Exception:
            pass

    async def run(self, command):
        attempt = command["attempt_id"]
        if not isinstance(attempt, str) or len(attempt) != 32 or attempt in self.seen:
            raise PreviewError("protocol_error")
        self.seen.add(attempt)
        if len(self.seen) > 4096:
            self.seen.clear()
        operation = command["operation"]
        source = command.get("source") or {}
        root = await disk(Path(source["root"]).resolve, strict=operation != "present")
        if operation == "walk_start":
            if not await disk(root.is_dir):
                raise ValueError("external root is not a directory")
            token = uuid.uuid4().hex
            self.walks[token] = _walk(root, bool(source.get("show_hidden")))
            data = {"token": token}
        elif operation == "walk_next":
            token = source["token"]
            iterator = self.walks[token]

            def page():
                entries = []
                for _ in range(PAGE_SIZE):
                    try:
                        entries.append(next(iterator))
                    except StopIteration:
                        return entries, True
                return entries, False

            entries, done = await disk(page)
            if done:
                self.walks.pop(token, None)
            data = {"entries": entries, "done": done}
        elif operation == "walk_end":
            self.walks.pop(source["token"], None)
            data = {"ended": True}
        elif operation == "prepare":
            path = Path(source["path"])
            result = await disk(prepare_file, path, root=root, display_filename=source.get("filename"))
            data = result.to_wire()
        elif operation == "hash":
            data = await disk(hash_file, Path(source["path"]), root=root)
        elif operation == "present":
            paths = source["paths"]
            if not isinstance(paths, list) or len(paths) > 64:
                raise PreviewError("protocol_error")
            if any(not Path(path).resolve().is_relative_to(root) for path in paths):
                raise PreviewError("protocol_error")
            # A file OR a directory: the scan asks this of its folder rows too, and a directory that
            # read as absent lost its row (and its product links) while it was still on the share.
            data = {"present": await disk(lambda: [Path(path).exists() for path in paths])}
        else:
            raise PreviewError("protocol_error")
        output = self.staging / f"{attempt}.json"
        try:
            await disk(output.write_bytes, json.dumps(data, ensure_ascii=False).encode("utf-8"))
            ref = await disk(describe, output, attempt, command["deadline_ns"], "library")
            await put(self.store, output, ref, command["deadline_ns"])
            return {"outcome": "ok", "attempt_id": attempt, "artifact": ref.wire()}
        finally:
            await disk(output.unlink, missing_ok=True)


async def main():
    config = json.loads(input())
    service = LibraryFileService(config)
    await service.start()
    try:
        while service.nc.is_connected:
            await asyncio.sleep(0.5)
    finally:
        await service.nc.close()


if __name__ == "__main__":
    asyncio.run(main())
