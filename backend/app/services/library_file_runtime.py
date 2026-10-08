"""Lifespan-owned library file worker on the existing local NATS broker."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

from backend.app.services.analysis_transport import AnalysisArtifact, get
from backend.app.services.library_file_preparation import PreparedLibraryFile
from backend.app.services.local_worker_broker import get_local_worker_broker
from backend.app.services.preview_artifacts import disk, owned
from backend.app.services.preview_process import PreviewProcess
from backend.app.services.preview_protocol import decode, encode
from backend.app.services.worker_staging import cleanup_owned

logger = logging.getLogger(__name__)
_REQUEST_SECONDS = 180
BUCKET_BYTES = 256 * 1024 * 1024


class LibraryFileRuntime:
    def __init__(self, base: Path, broker):
        self.base = base
        self.broker = broker
        self.generation = uuid.uuid4().hex
        self.epoch = uuid.uuid4().hex
        self.bucket = f"bamdude_library_{self.generation}"
        self.root = base / ".cache" / "library-file-service"
        self.slot = asyncio.Lock()
        self.active_task = self.interrupted_task = None
        self.worker = self.nc = self.store = self.monitor = None
        self.ready = self.closed = self.uncertain = False
        self.reason = "not_started"

    def health(self):
        return {"state": "ready" if self.ready and not self.closed else "unavailable", "reason": self.reason}

    async def start(self):
        import nats
        from nats.js.api import ObjectStoreConfig

        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.nc = await nats.connect(
                self.broker.url,
                token=self.broker.token,
                allow_reconnect=False,
                connect_timeout=5,
                closed_cb=self.disconnected,
            )
            js = self.nc.jetstream(timeout=5)
            # The broker's store outlives the process and each bucket reserves
            # max_bytes against max_file_store, so a previous generation's
            # bucket must go before this one is created.
            for stream in await js.streams_info():
                if stream.config.name.startswith("OBJ_bamdude_library_"):
                    await js.delete_stream(stream.config.name)
            self.store = await js.create_object_store(
                bucket=self.bucket,
                config=ObjectStoreConfig(bucket=self.bucket, max_bytes=BUCKET_BYTES, ttl=900, storage="file"),
            )
            await self.launch()
            self.monitor = asyncio.create_task(self.watch(), name="library-file-service-monitor")
        except Exception:
            self.reason = "startup_failed"
            await self.retire()
            if self.nc:
                await self.nc.close()
            raise

    async def launch(self):
        self.epoch = uuid.uuid4().hex
        self.worker = await disk(
            PreviewProcess,
            "backend.app.library_file_service",
            {
                "url": self.broker.url,
                "token": self.broker.token,
                "bucket": self.bucket,
                "generation": self.generation,
                "epoch": self.epoch,
                "staging": str(self.root / "staging" / self.generation),
            },
            self.root / "cache",
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.worker.process.poll() is not None:
                raise RuntimeError("library file worker exited")
            try:
                reply = await self.rpc({"generation": self.generation, "epoch": self.epoch, "operation": "ready"}, 2)
                if reply.get("outcome") == "ok" and reply.get("epoch") == self.epoch:
                    self.ready, self.reason = True, None
                    logger.info("Library file worker ready pid=%s", self.worker.process.pid)
                    return
            except Exception:
                pass
            await asyncio.sleep(0.1)
        raise RuntimeError("library file worker startup timeout")

    async def rpc(self, command: dict, timeout: float):
        message = await self.nc.request(
            f"bamdude.library.{self.generation}.{self.epoch}",
            encode(command),
            timeout=timeout,
        )
        return decode(message.data)

    async def disconnected(self):
        self.ready, self.reason = False, "broker_disconnected"
        if self.active_task:
            self.interrupted_task = self.active_task
            self.active_task.cancel()

    async def retire(self):
        self.ready = False
        worker, self.worker = self.worker, None
        if worker:
            try:
                await disk(worker.stop)
            except Exception:
                self.uncertain, self.reason = True, "ownership_uncertain"
                raise
            result = await disk(cleanup_owned, self.root / "staging" / self.generation)
            if result.status == "retained_error":
                logger.warning("library worker staging retained: %s", result.path)

    async def watch(self):
        while not self.closed:
            await asyncio.sleep(0.5)
            worker = self.worker
            dead = worker is None or worker.process.poll() is not None
            over_budget = bool(worker and not dead and await disk(worker.rss) > 1024**3)
            if not dead and not over_budget:
                continue
            self.ready = False
            if self.active_task:
                if self.interrupted_task is self.active_task:
                    continue
                self.interrupted_task = self.active_task
                self.active_task.cancel()
                continue
            if self.slot.locked() or self.uncertain or not self.nc.is_connected:
                continue
            try:
                await self.retire()
                await self.launch()
            except Exception:
                self.reason = "worker_restarting"
                logger.warning("library file worker restart failed", exc_info=True)
                await asyncio.sleep(2)

    async def request(self, operation: str, source: dict) -> dict:
        if not self.ready or self.closed:
            raise RuntimeError("library file service unavailable")
        async with self.slot:
            if not self.ready or self.worker.process.poll() is not None:
                raise RuntimeError("library file service unavailable")
            attempt = uuid.uuid4().hex
            output = self.root / "results" / f"{attempt}.json"
            command = {
                "generation": self.generation,
                "epoch": self.epoch,
                "operation": operation,
                "attempt_id": attempt,
                "deadline_ns": time.monotonic_ns() + _REQUEST_SECONDS * 10**9,
                "source": source,
            }
            started = time.monotonic()
            file_name = source.get("filename") or Path(source.get("path", "")).name
            outcome = "failed"
            failure = None
            if operation == "prepare":
                logger.info("Library file worker parsing file=%r attempt=%s", file_name, attempt[:8])
            self.active_task = asyncio.current_task()
            try:
                reply = await self.rpc(command, _REQUEST_SECONDS)
                if reply.get("outcome") != "ok" or reply.get("attempt_id") != attempt:
                    raise RuntimeError(f"library file service {reply.get('reason', reply.get('outcome'))}")
                ref = AnalysisArtifact.parse(reply["artifact"], attempt, "library")
                await disk(output.parent.mkdir, parents=True, exist_ok=True)
                await get(self.store, ref, output, time.monotonic_ns() + 30 * 10**9)
                data = json.loads(await disk(output.read_text, encoding="utf-8"))
                outcome = "ok"
                return data
            except (TimeoutError, asyncio.CancelledError) as exc:
                outcome = "canceled" if isinstance(exc, asyncio.CancelledError) else "timeout"
                failure = type(exc).__name__
                # A blocked SMB syscall cannot be canceled in a thread. The
                # guarded process must be reaped before another operation.
                try:
                    await owned(self.retire())
                except asyncio.CancelledError:
                    if self.interrupted_task is asyncio.current_task():
                        raise RuntimeError("library file service unavailable") from exc
                    raise
                if self.interrupted_task is asyncio.current_task():
                    raise RuntimeError("library file service unavailable") from exc
                raise
            except Exception as exc:
                failure = str(exc) or type(exc).__name__
                raise
            finally:
                if operation == "prepare":
                    plates = data.get("metadata", {}).get("plates") if outcome == "ok" else None
                    log = logger.info if outcome == "ok" else logger.warning
                    log(
                        "Library file worker parse file=%r attempt=%s outcome=%s plates=%s elapsed_ms=%d%s",
                        file_name,
                        attempt[:8],
                        outcome,
                        len(plates) if isinstance(plates, list) else "-",
                        (time.monotonic() - started) * 1000,
                        f" reason={failure!r}" if failure else "",
                    )
                try:
                    await self.store.delete(f"{attempt}_library")
                except Exception:
                    pass
                await disk(output.unlink, missing_ok=True)
                self.active_task = None
                self.interrupted_task = None

    async def prepare(self, path: Path, *, root: Path, filename: str | None = None) -> PreparedLibraryFile:
        data = await self.request("prepare", {"path": str(path), "root": str(root), "filename": filename})
        return PreparedLibraryFile.from_wire(data)

    async def hash(self, path: Path, *, root: Path) -> dict:
        return await self.request("hash", {"path": str(path), "root": str(root)})

    async def walk_start(self, root: Path, show_hidden: bool) -> str:
        data = await self.request("walk_start", {"root": str(root), "show_hidden": show_hidden})
        return data["token"]

    async def walk_next(self, root: Path, token: str) -> dict:
        return await self.request("walk_next", {"root": str(root), "token": token})

    async def walk_end(self, root: Path, token: str) -> None:
        await self.request("walk_end", {"root": str(root), "token": token})

    async def present(self, root: Path, paths: list[str]) -> list[bool]:
        return (await self.request("present", {"root": str(root), "paths": paths}))["present"]

    async def stop(self):
        self.closed = True
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        if self.active_task and self.active_task is not asyncio.current_task():
            self.active_task.cancel()
            await asyncio.gather(self.active_task, return_exceptions=True)
        async with self.slot:
            await self.retire()
            if not self.uncertain:
                result = await disk(cleanup_owned, self.root / "results")
                if result.status == "retained_error":
                    logger.warning("library worker results retained: %s", result.path)
        if self.nc:
            await self.nc.close()


runtime: LibraryFileRuntime | None = None


def get_library_file_health():
    return runtime.health() if runtime else {"state": "unavailable", "reason": "not_started"}


async def start_library_file_runtime(base: Path):
    global runtime
    broker = get_local_worker_broker()
    if broker is None:
        raise RuntimeError("local worker broker unavailable")
    runtime = LibraryFileRuntime(base, broker)
    await runtime.start()


async def stop_library_file_runtime():
    global runtime
    if runtime:
        await runtime.stop()
        runtime = None


def get_library_file_runtime() -> LibraryFileRuntime:
    if runtime is None:
        raise RuntimeError("library file service unavailable")
    return runtime
