"""Lifespan-owned, local-only broker/service and bounded preview admission."""

from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from backend.app.schemas.system import PreviewHealth
from backend.app.services.preview_artifacts import describe, disk, get, owned, put, snapshot
from backend.app.services.preview_process import PreviewProcess
from backend.app.services.preview_protocol import (
    ACTIVE_SECONDS,
    ATTEMPT_BYTES,
    BUCKET_BYTES,
    CONTROL_SECONDS,
    OUTCOMES,
    QUEUE_LIMIT,
    QUEUE_SECONDS,
    STARTUP_SECONDS,
    TTL_SECONDS,
    Artifact,
    Command,
    PreviewError,
    decode,
    encode,
    remaining,
)

logger = logging.getLogger(__name__)
RECOVERY_GUIDE = "https://docs.bamdude.top/reference/troubleshooting/#local-preview-service"


@dataclass
class PreviewResult:
    outcome: str = "unavailable"
    files: dict[str, Path] = field(default_factory=dict)
    input_digests: dict[str, str] = field(default_factory=dict)


class PreviewRuntime:
    def __init__(self, root: Path):
        self.root = root
        self.generation = uuid.uuid4().hex
        self.epoch = uuid.uuid4().hex
        self.bucket = f"bamdude_preview_{self.generation}"
        self.broker = self.nc = self.service = self.store = None
        self.ready = False
        self.closed = False
        self.uncertain = False
        self.sequence = 0
        self.waiters = 0
        self.slot = asyncio.Lock()
        self.active_task = None
        self.monitor = None
        self.restarts = []
        self.circuit_until = 0.0
        self.dependency_lost = False
        self.reason = "not_started"
        self.error_type = None

    def health(self) -> PreviewHealth:
        """In-memory snapshot only: never probe processes, disk or the broker here."""
        if self.uncertain:
            state, reason = "unavailable", "ownership_uncertain"
        elif self.ready and not self.closed:
            state, reason = "ready", None
        elif self.reason == "starting":
            state, reason = "starting", self.reason
        elif self.reason == "worker_restarting" and not self.closed:
            state = "recovering"
            reason = "circuit_open" if time.monotonic() < self.circuit_until else self.reason
        else:
            state, reason = "unavailable", self.reason
        return PreviewHealth(
            state=state,
            reason=reason,
            error_type=self.error_type,
            runtime_dir=str(self.root),
            recovery_required=reason in {"recovery_required", "ownership_uncertain"},
        )

    def _unavailable(self, reason: str, exc: Exception | None = None):
        self.ready = False
        error_type = type(exc).__name__ if exc else None
        changed = (reason, error_type) != (self.reason, self.error_type)
        self.reason, self.error_type = reason, error_type
        if changed:
            # Do not emit exception text: connection errors may contain credentials.
            logger.warning(
                "Preview unavailable: reason=%s error=%s runtime_dir=%s; API and printing continue. "
                "Inspect installation/disk/logs; for unresolved ownership stop BamDude, verify all preview "
                "processes exited and follow %s. Do not delete the runtime marker blindly.",
                reason,
                error_type or "none",
                self.root,
                RECOVERY_GUIDE,
            )

    async def start(self):
        self.reason = "starting"
        recovery_errors: tuple[type[Exception], ...] = ()
        try:
            import nats
            from embedded_nats import NatsServer, RecoveryRequired
            from nats.js.api import ObjectStoreConfig

            recovery_errors = (RecoveryRequired,)
            self.token = secrets.token_urlsafe(32)
            await disk(self._directories)
            self.broker = NatsServer(
                self.root / "broker",
                auth_token=self.token,
                max_file_store="3072MB",
                max_memory_store="64MB",
                max_payload=1024**2,
                startup_timeout=15,
                shutdown_timeout=2,
                recover_stale=True,
            )
            async with asyncio.timeout(STARTUP_SECONDS):
                await disk(self.broker.start)
                if self.broker.recovered_generation:
                    logger.warning(
                        "Preview broker recovered abandoned generation=%s; runtime_dir=%s. "
                        "Old preview staging is retained until manual ownership verification; see %s",
                        self.broker.recovered_generation,
                        self.root,
                        RECOVERY_GUIDE,
                    )
                self.nc = await nats.connect(
                    self.broker.url,
                    token=self.token,
                    allow_reconnect=False,
                    connect_timeout=CONTROL_SECONDS,
                    closed_cb=self.disconnected,
                )
                js = self.nc.jetstream(timeout=CONTROL_SECONDS)
                # A clean package start proves exclusive store ownership. Old
                # preview buckets are disposable; no other namespaces touched.
                streams = await js.streams_info()
                for stream in streams:
                    if stream.config.name.startswith("OBJ_bamdude_preview_"):
                        await js.delete_stream(stream.config.name)
                # Broker ownership says nothing about an old worker/renderer's
                # disk I/O. Even a later clean restart cannot prove it is gone.
                await disk(self._report_retained_staging)
                self.store = await js.create_object_store(
                    bucket=self.bucket,
                    config=ObjectStoreConfig(
                        bucket=self.bucket, max_bytes=BUCKET_BYTES, ttl=TTL_SECONDS, storage="file"
                    ),
                )
                await self._launch()
            self.monitor = asyncio.create_task(self._monitor(), name="preview-service-monitor")
        except Exception as exc:
            self._unavailable("recovery_required" if isinstance(exc, recovery_errors) else "startup_failed", exc)
            if isinstance(exc, recovery_errors):
                logger.warning(
                    "Preview broker recovery refused: reason=%s marker=%s; follow %s",
                    exc.reason,
                    exc.marker_path or (self.broker.recovery_marker_path if self.broker else None),
                    RECOVERY_GUIDE,
                )
            logger.debug("Preview startup failure", exc_info=True)
            await self.stop()

    def _directories(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.staging = self.root / "staging" / self.generation
        self.staging.mkdir(parents=True, mode=0o700)
        (self.staging / "main").mkdir(mode=0o700)
        (self.staging / "service").mkdir(mode=0o700)

    def _report_retained_staging(self):
        for directory in self.staging.parent.iterdir():
            if directory == self.staging or directory.is_symlink() or directory.is_junction():
                continue
            if len(directory.name) == 32 and all(c in "0123456789abcdef" for c in directory.name):
                logger.warning(
                    "Retained preview staging: %s; old worker/renderer ownership is unproven. "
                    "Stop all preview processes before manual cleanup; see %s",
                    directory,
                    RECOVERY_GUIDE,
                )

    def _command(self, operation="ready", manifest=(), attempt_id=None, deadline=None):
        self.sequence += 1
        return Command(
            1,
            self.generation,
            self.epoch,
            attempt_id or uuid.uuid4().hex,
            self.sequence,
            operation,
            deadline or time.monotonic_ns() + int(CONTROL_SECONDS * 1e9),
            tuple(manifest),
        )

    async def _launch(self):
        self.epoch = uuid.uuid4().hex

        async def spawn():
            self.service = await disk(
                PreviewProcess,
                "backend.app.preview_service",
                {
                    "url": self.broker.url,
                    "token": self.token,
                    "bucket": self.bucket,
                    "generation": self.generation,
                    "epoch": self.epoch,
                    "staging": str(self.staging / "service"),
                    "cache": str(self.root / "matplotlib"),
                },
                self.root / "matplotlib",
            )

        # Capture ownership even when cancellation lands during Popen/Job attach.
        await owned(spawn())
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.service.process.poll() is not None:
                raise PreviewError("unavailable")
            try:
                before = time.monotonic_ns()
                reply = await self._rpc(self._command())
                after = time.monotonic_ns()
                if (
                    reply.get("outcome") == "ok"
                    and reply.get("epoch") == self.epoch
                    and reply.get("protocol_version") == 1
                    and before <= reply.get("monotonic_ns", 0) <= after
                ):
                    self.ready = True
                    self.reason = None
                    self.error_type = None
                    return
            except Exception:
                pass
            await asyncio.sleep(0.1)
        raise PreviewError("unavailable")

    async def _rpc(self, command, operation=None, timeout=CONTROL_SECONDS):
        message = await self.nc.request(command.subject, encode(command.wire(operation)), timeout=timeout)
        return decode(message.data)

    async def disconnected(self):
        self.ready = False
        if not self.closed:
            self._unavailable("broker_disconnected")
        if self.active_task:
            self.dependency_lost = True
            self.active_task.cancel()

    async def _retire(self):
        self.ready = False
        if not self.closed and not self.uncertain and self.reason != "broker_disconnected":
            self.reason = "worker_restarting"
        if self.service:
            try:
                await disk(self.service.stop)
            except Exception:
                self.uncertain = True
                self._unavailable("ownership_uncertain")
                raise
            self.service = None

    async def _cancel(self, command):
        # Revoke in the caller first. A reply is accepted only after the service
        # has reaped its child and joined owned I/O; otherwise retire the tree.
        if command and self.nc.is_connected:
            try:
                reply = await self._rpc(command, "cancel")
                if reply.get("outcome") == "canceled":
                    return
            except Exception:
                pass
        await self._retire()

    async def _monitor(self):
        while not self.closed:
            await asyncio.sleep(0.5)
            if self.service and self.service.process.poll() is None and self.nc.is_connected:
                continue
            self.ready = False
            if self.active_task:
                self.dependency_lost = True
                self.active_task.cancel()
                continue
            if self.slot.locked() or self.uncertain:
                continue
            try:
                await self._retire()
            except Exception:
                logger.warning("Preview process ownership uncertain; restart disabled")
                return
            if not self.nc.is_connected:  # Broker recovery happens on the next application start, not in-flight.
                self._unavailable("broker_disconnected")
                return
            self.reason = "worker_restarting"
            now = time.monotonic()
            self.restarts = [x for x in self.restarts if now - x < 60]
            if now < self.circuit_until:
                continue
            if len(self.restarts) >= 3:
                self.circuit_until = now + 60
                continue
            await asyncio.sleep(2 ** len(self.restarts))
            self.restarts.append(time.monotonic())
            try:
                await self._launch()
            except Exception as exc:
                self._unavailable("worker_restarting", exc)
                try:
                    await self._retire()
                except Exception:
                    logger.warning("Preview process ownership uncertain; restart disabled")
                    return

    @asynccontextmanager
    async def attempt(self, inputs: dict[str, tuple[Path | bytes, str]]):
        result = PreviewResult()
        if not self.ready or self.closed or self.waiters >= QUEUE_LIMIT:
            result.outcome = "busy" if self.ready else "unavailable"
            yield result
            return
        self.waiters += 1
        admitted_epoch = self.epoch
        acquired = False
        try:
            async with asyncio.timeout(QUEUE_SECONDS):
                await self.slot.acquire()
                acquired = True
        except TimeoutError:
            result.outcome = "busy"
        finally:
            self.waiters -= 1
        if not acquired:
            yield result
            return
        attempt_id = uuid.uuid4().hex
        root = self.staging / "main" / attempt_id
        deadline = time.monotonic_ns() + int(ACTIVE_SECONDS * 1e9)
        command = None
        subscription = None
        revoked = False
        canceled = False
        checkpoint_tasks = set()
        transfer_nc = None
        began = time.monotonic()

        async def cleanup():
            if self.store and self.nc.is_connected:
                for role in ("mesh", "sliced", "source_png", "preview", "checkpoint", "output"):
                    try:
                        await self.store.delete(f"{attempt_id}_{role}")
                    except Exception:
                        pass  # bucket TTL bounds interrupted/partial uploads
            if not self.uncertain:
                await disk(shutil.rmtree, root, True)

        try:
            try:
                if not self.ready or self.closed or self.epoch != admitted_epoch:
                    raise PreviewError("unavailable")
                self.active_task = asyncio.current_task()
                self.dependency_lost = False
                # A previous denied cleanup must not accumulate unbounded
                # staging across attempts. No other attempt owns this slot.
                if await disk(lambda: any(p.is_file() for p in self.staging.rglob("*"))):
                    raise PreviewError("resource_limit")
                await disk(root.mkdir)
                manifest = []
                async with asyncio.timeout(remaining(deadline)):
                    import nats

                    transfer_nc = await nats.connect(
                        self.broker.url, token=self.token, allow_reconnect=False, connect_timeout=CONTROL_SECONDS
                    )
                    transfer_store = await transfer_nc.jetstream(timeout=CONTROL_SECONDS).object_store(self.bucket)
                    # Reserve a whole attempt plus metadata headroom. Never evict
                    # live objects to make room; the server's max_age sweeps orphan
                    # chunks as well as completed objects in this private bucket.
                    if (await transfer_store.status()).size + ATTEMPT_BYTES + 1024**2 > BUCKET_BYTES:
                        raise PreviewError("resource_limit")
                    for role, (source, kind) in inputs.items():
                        if role not in {"mesh", "sliced", "source_png"} or kind not in {"stl", "obj", "3mf", "png"}:
                            raise PreviewError("protocol_error")
                        path = root / f"{role}.{kind}"
                        await disk(snapshot, source, path, deadline)
                        ref = await disk(describe, path, attempt_id, role, kind, deadline)
                        manifest.append(ref)
                        result.input_digests[role] = ref.digest
                        if sum(x.size for x in manifest) > ATTEMPT_BYTES:
                            raise PreviewError("resource_limit")
                        await put(transfer_store, path, ref, deadline)
                    command = self._command("run", manifest, attempt_id, deadline)

                    async def receive_files(items):
                        if revoked:
                            raise PreviewError("canceled")
                        refs = [Artifact.parse(item, attempt_id) for item in items]
                        if len({r.role for r in refs}) != len(refs) or any(
                            r.role not in {"preview", "checkpoint", "output"} for r in refs
                        ):
                            raise PreviewError("protocol_error")
                        if sum(x.size for x in [*manifest, *refs]) > ATTEMPT_BYTES:
                            raise PreviewError("resource_limit")
                        for ref in refs:
                            if ref.role in result.files:
                                continue
                            path = root / f"{ref.role}.{ref.kind}"
                            await get(transfer_store, ref, path, deadline)
                            if revoked:
                                raise PreviewError("canceled")
                            result.files[ref.role] = path

                    async def checkpoint_work(message):
                        try:
                            payload = decode(message.data)
                            if payload.get("command") != command.wire("checkpoint-receipt"):
                                raise PreviewError("protocol_error")
                            await receive_files(payload["manifest"])
                            await message.respond(encode({"outcome": "ok"}))
                        except Exception:
                            if self.nc.is_connected:
                                await message.respond(encode({"outcome": "canceled"}))

                    async def checkpoint(message):
                        if checkpoint_tasks or revoked:
                            await message.respond(encode({"outcome": "canceled"}))
                            return
                        task = asyncio.create_task(checkpoint_work(message))
                        checkpoint_tasks.add(task)

                    subscription = await self.nc.subscribe(command.subject + ".checkpoint", cb=checkpoint)
                    await self.nc.flush(timeout=CONTROL_SECONDS)
                    reply = await self._rpc(command, timeout=remaining(deadline))
                    result.outcome = reply.get("outcome", "protocol_error")
                    if not isinstance(result.outcome, str) or result.outcome not in OUTCOMES:
                        raise PreviewError("protocol_error")
                    if result.outcome in {"unavailable", "timeout", "protocol_error"}:
                        raise PreviewError(result.outcome)
                    if result.outcome == "ok":
                        await receive_files(reply.get("manifest", []))
            except asyncio.CancelledError:
                revoked = True
                canceled = not self.dependency_lost
                if canceled:
                    result.files.clear()
                result.outcome = "canceled" if canceled else "unavailable"
                try:
                    await owned(self._cancel(command))
                except Exception:
                    self.uncertain = True
            except Exception as exc:
                revoked = True
                result.outcome = (
                    exc.outcome
                    if isinstance(exc, PreviewError)
                    else ("timeout" if isinstance(exc, TimeoutError) else "unavailable")
                )
                if command and result.outcome in {"unavailable", "timeout", "protocol_error"}:
                    try:
                        await owned(self._cancel(command))
                    except Exception:
                        self.uncertain = True
            finally:

                async def close_transfers():
                    if subscription:
                        try:
                            await subscription.unsubscribe()
                        except Exception:
                            pass  # closing the dead connection already removed it
                    for task in checkpoint_tasks:
                        task.cancel()
                    if checkpoint_tasks:
                        await asyncio.gather(*checkpoint_tasks, return_exceptions=True)
                    if transfer_nc:
                        try:
                            await transfer_nc.close()
                        except Exception:
                            pass

                try:
                    await owned(close_transfers())
                except asyncio.CancelledError:
                    revoked = canceled = True
                    result.files.clear()
                    result.outcome = "canceled"
                    try:
                        await owned(self._retire())
                    except BaseException:
                        self.uncertain = True
            if canceled:
                raise asyncio.CancelledError
            yield result  # main validates/publishes while permit and staging still owned
        finally:

            async def finish():
                try:
                    await cleanup()
                finally:
                    self.active_task = None
                    if not self.uncertain:
                        self.slot.release()

            await owned(finish())
            logger.info(
                "Preview attempt=%s outcome=%s elapsed_ms=%d",
                attempt_id,
                result.outcome,
                (time.monotonic() - began) * 1000,
            )

    async def stop(self):
        self.closed = True
        self.ready = False
        if self.reason in {None, "starting", "worker_restarting", "not_started"}:
            self.reason = "stopped"
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        if self.active_task and self.active_task is not asyncio.current_task():
            self.active_task.cancel()
            await asyncio.gather(self.active_task, return_exceptions=True)
        try:
            await self._retire()
        except Exception:
            logger.warning("Preview process ownership uncertain; staging retained")
        if self.nc:
            await self.nc.close()
        if self.broker:
            try:
                await disk(self.broker.stop)
            except Exception as exc:
                self._unavailable("recovery_required", exc)
        if not self.uncertain and hasattr(self, "staging") and not self.slot.locked():
            await disk(shutil.rmtree, self.staging, True)


runtime: PreviewRuntime | None = None


def get_preview_health() -> PreviewHealth:
    return runtime.health() if runtime else PreviewHealth(state="unavailable", reason="not_started")


async def start_preview_runtime(base: Path):
    global runtime
    runtime = PreviewRuntime(base / ".cache" / "preview-service")
    await runtime.start()


async def stop_preview_runtime():
    global runtime
    if runtime:
        await runtime.stop()
        runtime = None


@asynccontextmanager
async def preview_attempt(inputs):
    if runtime is None:
        yield PreviewResult()
    else:
        async with runtime.attempt(inputs) as result:
            yield result
