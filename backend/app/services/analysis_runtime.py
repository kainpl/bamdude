"""Lifespan-owned NATS parser transport; print-context cache stays in main."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path

from backend.app.services.analysis_codec import decode_file
from backend.app.services.analysis_source import resolve_source
from backend.app.services.analysis_transport import BUCKET_BYTES, STAGING_BYTES, TTL_SECONDS, AnalysisArtifact, get
from backend.app.services.local_worker_broker import get_local_worker_broker
from backend.app.services.preview_artifacts import disk, owned
from backend.app.services.preview_process import PreviewProcess
from backend.app.services.preview_protocol import CONTROL_SECONDS, PreviewError, decode, encode
from backend.app.services.worker_staging import abandoned_attempts, cleanup_owned, retained_entries

logger = logging.getLogger(__name__)
# Compute time grows with the G-code limit (owner, 2026-11-20): 180 s for
# 256 MiB became 720 s for 1 GiB (``print_file_analysis._MAX_GCODE_BYTES``).
# The transfer moves the small result, not the G-code, and keeps its 60 s.
_COMPUTE_SECONDS = 720
_TRANSFER_SECONDS = 60
_HARD_SECONDS = _COMPUTE_SECONDS + _TRANSFER_SECONDS
# Bootstrap does disk-backed JetStream administration, not a worker control RPC.
_BOOTSTRAP_SECONDS = 30
_BOOTSTRAP_REQUEST_SECONDS = 10
_BOOTSTRAP_ATTEMPTS = 3
_BOOTSTRAP_RETRY_SECONDS = 0.5


class AnalysisRuntime:
    def __init__(self, base: Path, broker):
        self.base = base
        self.root = base / ".cache" / "analysis-service"
        self.broker = broker
        self.generation = uuid.uuid4().hex
        self.epoch = uuid.uuid4().hex
        self.bucket = f"bamdude_analysis_{self.generation}"
        self.sequence = 0
        self.slot = asyncio.Lock()
        self.active_task = None
        self.interrupted_task = None
        self.nc = self.store = self.service = self.monitor = None
        self.startup = self._stop_task = None
        self._startup_phase = "not_started"
        self.ready = self.closed = self.uncertain = False
        self.reason = "not_started"
        self._last_staging_issue = None
        self._last_admission_issue = None
        self.restarts = []
        self.circuit_until = 0.0
        self.stats = {
            "attempts": 0,
            "ok": 0,
            "failed": 0,
            "canceled": 0,
            "service_restart_attempts": 0,
            "last_compute_ms": None,
            "last_transfer_ms": None,
            "last_artifact_bytes": None,
        }

    def health(self) -> dict:
        return {
            "state": "ready" if self.ready and not self.closed else "unavailable",
            "reason": self.reason,
            "transport": dict(self.stats),
        }

    async def start(self):
        if self.closed:
            return
        if self.startup is None:
            self.startup = asyncio.create_task(self._start(), name="analysis-service-startup")
        try:
            await asyncio.shield(self.startup)
        except asyncio.CancelledError:
            await owned(self.stop())
            raise
        if not self.ready and self.monitor is None:
            await self.stop()

    async def _start(self):
        self.reason = "starting"
        started = time.monotonic()
        self._startup_phase = "staging"
        logger.info("3MF analysis startup: generation=%s", self.generation)
        try:
            if self.broker is None:
                raise RuntimeError("local broker unavailable")
            self.staging = self.root / "staging" / self.generation
            await disk(self.staging.mkdir, parents=True, exist_ok=True, mode=0o700)
            await disk((self.staging / "main").mkdir)
            await disk((self.staging / "service").mkdir)
            await disk(self._report_retained_staging)
            async with asyncio.timeout(_BOOTSTRAP_SECONDS):
                await self._bootstrap_transport()
            self._startup_phase = "worker_ready"
            await self.launch()
            logger.info(
                "3MF analysis startup_ready: generation=%s elapsed_ms=%.1f",
                self.generation,
                (time.monotonic() - started) * 1000,
            )
            self.monitor = asyncio.create_task(self._monitor(), name="analysis-service-monitor")
        except asyncio.CancelledError:
            logger.info(
                "3MF analysis startup_canceled: generation=%s phase=%s elapsed_ms=%.1f",
                self.generation,
                self._startup_phase,
                (time.monotonic() - started) * 1000,
            )
            raise
        except Exception as exc:
            self.reason = "startup_failed"
            logger.warning(
                "3MF analysis service unavailable: generation=%s phase=%s error=%s elapsed_ms=%.1f",
                self.generation,
                self._startup_phase,
                type(exc).__name__,
                (time.monotonic() - started) * 1000,
            )
            if self.nc and self.nc.is_connected and self.store is not None:
                # A transient service/child startup failure must not make the
                # parser unavailable until the whole application is restarted.
                try:
                    await self.retire()
                except Exception:
                    pass
                if not self.uncertain:
                    self.monitor = asyncio.create_task(self._monitor(), name="analysis-service-monitor")

    async def _bootstrap_transport(self):
        from nats.aio.client import Client
        from nats.errors import ConnectionClosedError, NoRespondersError, NoServersError, StaleConnectionError
        from nats.js.api import ObjectStoreConfig
        from nats.js.errors import ServiceUnavailableError

        for attempt in range(1, _BOOTSTRAP_ATTEMPTS + 1):
            started = time.monotonic()
            # Retain the client BEFORE connect: shutdown also owns a connection
            # whose handshake has not returned yet.
            client = self.nc = Client()

            async def disconnected(client=client):
                if self.nc is client:
                    await self.disconnected()

            async def error(exc):
                logger.debug("3MF analysis transport error: error=%s", type(exc).__name__)

            try:
                self._startup_phase = "connect"
                await client.connect(
                    self.broker.url,
                    token=self.broker.token,
                    allow_reconnect=False,
                    connect_timeout=_BOOTSTRAP_REQUEST_SECONDS,
                    closed_cb=disconnected,
                    error_cb=error,
                )
                js = client.jetstream(timeout=_BOOTSTRAP_REQUEST_SECONDS)
                self._startup_phase = "stream_list"
                for stream in await js.streams_info():
                    if stream.config.name.startswith("OBJ_bamdude_analysis_"):
                        self._startup_phase = "stream_delete"
                        await js.delete_stream(stream.config.name)
                self._startup_phase = "store_create"
                # A timed-out CREATE may already have succeeded. Before any
                # worker/admission exists, the next attempt sweeps our namespace
                # again and recreates this SAME bucket, never another consumer's.
                self.store = await js.create_object_store(
                    bucket=self.bucket,
                    config=ObjectStoreConfig(
                        bucket=self.bucket, max_bytes=BUCKET_BYTES, ttl=TTL_SECONDS, storage="file"
                    ),
                )
                return
            except (
                OSError,
                TimeoutError,
                ConnectionClosedError,
                NoRespondersError,
                ServiceUnavailableError,
                NoServersError,
                StaleConnectionError,
            ) as exc:
                await self._close_transport()
                if attempt == _BOOTSTRAP_ATTEMPTS:
                    raise
                logger.warning(
                    "3MF analysis startup_retry: generation=%s attempt=%d phase=%s error=%s elapsed_ms=%.1f",
                    self.generation,
                    attempt,
                    self._startup_phase,
                    type(exc).__name__,
                    (time.monotonic() - started) * 1000,
                )
                await asyncio.sleep(_BOOTSTRAP_RETRY_SECONDS * attempt)

    async def _close_transport(self):
        # Detach first: the intentional close callback is not a broker failure.
        client, self.nc = self.nc, None
        self.store = None
        if client is not None:
            await owned(client.close())

    def _report_retained_staging(self):
        empty = 0
        for path, classification, reason in retained_entries(self.staging.parent, self.staging, "analysis"):
            if classification == "empty_skeleton":
                empty += 1
                logger.debug("Analysis staging skeleton retained: path=%s", path)
            else:
                logger.warning(
                    "Retained analysis staging: path=%s classification=%s reason=%s; verify owners before manual cleanup",
                    path,
                    classification,
                    reason,
                )
        if empty:
            logger.info("Analysis staging empty_skeleton_count=%d; no manual cleanup required", empty)

    def _cleanup_result(self, result, phase: str, attempt: str | None = None):
        if result.status == "retained_error":
            issue = (phase, str(result.path), result.error_type, result.error_code)
            log = logger.warning if issue != self._last_staging_issue else logger.debug
            log(
                "Analysis staging_cleanup_failed: phase=%s attempt=%s path=%s error=%s code=%s",
                phase,
                attempt,
                result.path,
                result.error_type,
                result.error_code,
            )
            self._last_staging_issue = issue
        else:
            self._last_staging_issue = None

    def _service_cleanup_reply(self, reply, attempt):
        detail = reply.get("staging_cleanup") if isinstance(reply, dict) else None
        if isinstance(detail, dict):
            logger.warning(
                "Analysis staging_cleanup_failed: phase=service_attempt attempt=%s path=%s error=%s code=%s",
                attempt,
                detail.get("path"),
                detail.get("error"),
                detail.get("code"),
            )

    def command(self, operation: str, attempt_id: str | None = None, source: dict | None = None, deadline: int = 0):
        self.sequence += 1
        return {
            "version": 1,
            "generation": self.generation,
            "epoch": self.epoch,
            "attempt_id": attempt_id or uuid.uuid4().hex,
            "sequence": self.sequence,
            "operation": operation,
            "deadline_ns": deadline or time.monotonic_ns() + int(CONTROL_SECONDS * 1e9),
            "source": source,
        }

    async def rpc(self, command: dict, timeout: float):
        message = await self.nc.request(
            f"bamdude.analysis.{self.generation}.{self.epoch}",
            encode(command),
            timeout=timeout,
        )
        return decode(message.data)

    async def launch(self):
        self.epoch = uuid.uuid4().hex

        async def spawn():
            self.service = await disk(
                PreviewProcess,
                "backend.app.analysis_service",
                {
                    "url": self.broker.url,
                    "token": self.broker.token,
                    "bucket": self.bucket,
                    "generation": self.generation,
                    "epoch": self.epoch,
                    "staging": str(self.staging / "service"),
                    "archive_root": str(self.base / "archive"),
                },
                self.root / "cache",
            )

        await owned(spawn())
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.service.process.poll() is not None:
                raise PreviewError("unavailable")
            try:
                before = time.monotonic_ns()
                reply = await self.rpc(self.command("ready"), CONTROL_SECONDS)
                after = time.monotonic_ns()
                if (
                    reply.get("outcome") == "ok"
                    and reply.get("epoch") == self.epoch
                    and reply.get("version") == 1
                    and before <= reply.get("monotonic_ns", 0) <= after
                ):
                    self.ready, self.reason = True, None
                    return
            except Exception:
                pass
            await asyncio.sleep(0.1)
        raise PreviewError("unavailable")

    async def disconnected(self):
        if self.closed:
            return
        self.ready = False
        self.reason = "broker_disconnected"
        if self.active_task and self.interrupted_task is not self.active_task:
            self.interrupted_task = self.active_task
            self.active_task.cancel()

    async def retire(self):
        self.ready = False
        if self.service:
            retired_epoch = self.epoch
            service = self.service
            try:
                await disk(service.stop)
            except Exception:
                self.uncertain = True
                self.reason = "ownership_uncertain"
                raise
            self.service = None
            attempts, unknown = await disk(abandoned_attempts, self.staging / "service")
            deadline = time.monotonic() + 2.0
            for path in unknown:
                logger.warning(
                    "Analysis service residue unknown: generation=%s epoch=%s path=%s",
                    self.generation,
                    retired_epoch,
                    path,
                )
            for path in attempts:
                result = await disk(cleanup_owned, path, deadline=deadline)
                logger.info(
                    "Analysis service_residue_after_retire: generation=%s epoch=%s path=%s result=%s",
                    self.generation,
                    retired_epoch,
                    path,
                    result.status,
                )
                self._cleanup_result(result, "service_residue_after_retire", path.name)

    async def _monitor(self):
        while not self.closed:
            await asyncio.sleep(0.5)
            if self.service and self.service.process.poll() is None and self.nc.is_connected:
                continue
            self.ready = False
            if self.active_task:
                if self.interrupted_task is self.active_task:
                    continue
                self.interrupted_task = self.active_task
                self.active_task.cancel()
                continue
            if self.slot.locked() or self.uncertain:
                continue
            try:
                await self.retire()
            except Exception:
                return
            if not self.nc.is_connected:
                self.reason = "broker_disconnected"
                return
            self.reason = "worker_restarting"
            now = time.monotonic()
            self.restarts = [value for value in self.restarts if now - value < 60]
            if now < self.circuit_until:
                continue
            if len(self.restarts) >= 3:
                self.circuit_until = now + 60
                continue
            await asyncio.sleep(2 ** len(self.restarts))
            self.restarts.append(time.monotonic())
            self.stats["service_restart_attempts"] += 1
            try:
                await self.launch()
            except Exception as exc:
                logger.warning("3MF analysis service restart failed: %s", type(exc).__name__)
                try:
                    await self.retire()
                except Exception:
                    return

    async def cancel(self, command: dict):
        if self.nc and self.nc.is_connected:
            try:
                reply = await self.rpc({**command, "operation": "cancel"}, CONTROL_SECONDS)
                self._service_cleanup_reply(reply, command["attempt_id"])
                if reply.get("outcome") == "canceled":
                    return
            except Exception:
                pass
        await self.retire()

    async def parse(self, path: Path, plate_id: int | None):
        from backend.app.services.print_file_analysis import AnalysisResourceError

        if not self.ready or self.closed:
            raise AnalysisResourceError("analysis service unavailable")
        async with self.slot:
            if not self.ready or self.closed:
                raise AnalysisResourceError("analysis service unavailable")
            attempt = uuid.uuid4().hex
            try:
                source = await disk(
                    resolve_source,
                    base_dir=self.base,
                    archive_dir=self.base / "archive",
                    archive_file_path=str(path),
                    plate_id=plate_id,
                    token=attempt,
                )
            except Exception as exc:
                raise AnalysisResourceError("archive source unavailable") from exc
            root = self.staging / "main" / attempt
            deadline = time.monotonic_ns() + _COMPUTE_SECONDS * 10**9
            hard_deadline = time.monotonic() + _HARD_SECONDS
            command = self.command("run", attempt, source.to_payload(), deadline)
            self.stats["attempts"] += 1
            self.active_task = asyncio.current_task()
            command_completed = False
            try:
                await disk(root.mkdir)
                used_bytes = await disk(lambda: sum(p.stat().st_size for p in self.staging.rglob("*") if p.is_file()))
                if used_bytes > STAGING_BYTES:
                    issue = (str(self.staging), STAGING_BYTES)
                    log = logger.warning if issue != self._last_admission_issue else logger.debug
                    log(
                        "analysis_staging_budget_exceeded: used_bytes=%d limit_bytes=%d path=%s",
                        used_bytes,
                        STAGING_BYTES,
                        self.staging,
                    )
                    self._last_admission_issue = issue
                    raise AnalysisResourceError("analysis staging budget exceeded")
                self._last_admission_issue = None
                reply = await self.rpc(command, _HARD_SECONDS)
                self._service_cleanup_reply(reply, attempt)
                command_completed = True
                if reply.get("outcome") != "ok":
                    raise AnalysisResourceError(f"analysis worker {reply.get('outcome', 'unavailable')}")
                complete = reply.get("compute_complete_ns")
                if (
                    type(complete) is not int
                    or complete > time.monotonic_ns()
                    or complete < deadline - _COMPUTE_SECONDS * 10**9
                ):
                    raise AnalysisResourceError("invalid analysis phase marker")
                transfer_end = min(
                    hard_deadline,
                    time.monotonic() + _TRANSFER_SECONDS,
                    time.monotonic() + max(0.0, (complete + _TRANSFER_SECONDS * 10**9 - time.monotonic_ns()) / 1e9),
                )
                ref = AnalysisArtifact.parse(reply.get("artifact"), attempt)
                output = root / "analysis.bin"
                await get(self.store, ref, output, int(transfer_end * 1e9))
                result = await disk(decode_file, output, identity=attempt, size=ref.size, sha256=ref.digest)
                self.stats["ok"] += 1
                self.stats["last_compute_ms"] = round((complete - (deadline - _COMPUTE_SECONDS * 10**9)) / 1e6, 3)
                self.stats["last_transfer_ms"] = round((time.monotonic_ns() - complete) / 1e6, 3)
                self.stats["last_artifact_bytes"] = ref.size
                return result
            except asyncio.CancelledError as exc:
                interrupted = self.interrupted_task is asyncio.current_task()
                self.stats["failed" if interrupted else "canceled"] += 1
                try:
                    if not command_completed:
                        await owned(self.cancel(command))
                except Exception:
                    self.uncertain = True
                if interrupted:
                    raise AnalysisResourceError("analysis service unavailable") from exc
                raise
            except Exception as exc:
                self.stats["failed"] += 1
                if isinstance(exc, (TimeoutError, PreviewError)):
                    try:
                        await owned(self.cancel(command))
                    except Exception:
                        self.uncertain = True
                # The parent log must say why (local microservice rule): our own
                # refusals carry a safe, path-free text (the worker's outcome,
                # a phase marker, a budget); anything else is named by its type.
                reason = str(exc) if isinstance(exc, AnalysisResourceError) else type(exc).__name__
                raise AnalysisResourceError(f"analysis service failed: {reason}") from exc
            finally:

                async def cleanup():
                    try:
                        if self.store and self.nc and self.nc.is_connected:
                            await self.store.delete(f"{attempt}_analysis")
                    except Exception:
                        pass  # orphan is bounded by bucket TTL
                    if not self.uncertain:
                        self._cleanup_result(await disk(cleanup_owned, root), "main_attempt", attempt)

                try:
                    await owned(cleanup())
                finally:
                    if self.interrupted_task is asyncio.current_task():
                        self.interrupted_task = None
                    self.active_task = None

    async def stop(self):
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop(), name="analysis-service-stop")
        await owned(self._stop_task)

    async def _stop(self):
        self.closed = True
        self.ready = False
        if self.reason in (None, "starting", "worker_restarting"):
            self.reason = "stopped"
        if self.startup and not self.startup.done():
            self.startup.cancel()
            await asyncio.gather(self.startup, return_exceptions=True)
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        if self.active_task and self.active_task is not asyncio.current_task():
            self.active_task.cancel()
            await asyncio.gather(self.active_task, return_exceptions=True)
        try:
            await self.retire()
        except Exception:
            logger.exception("3MF analysis worker ownership uncertain; staging retained")
        await self._close_transport()
        if not self.uncertain and hasattr(self, "staging") and not self.slot.locked():
            self._cleanup_result(await disk(cleanup_owned, self.staging), "generation")


runtime: AnalysisRuntime | None = None
_start_expected = False
_started_once = False


def expect_start() -> None:
    """The lifespan says, before printers connect, that it will start the service."""
    global _start_expected, _started_once
    _start_expected, _started_once = True, False


def service_start_pending() -> bool:
    """Whether the lifespan has announced the service and its first start has not finished yet.

    Printers reconnect before the lifespan reaches the analysis service, so a request made then
    has met no failure: the service is not there *yet*. After the first start has finished --
    ready or failed -- an unavailable service is an ordinary failure again. A process that
    announced no start (a test, a script) is never pending.
    """
    return _start_expected and not _started_once


async def wait_for_first_start(timeout: float) -> bool:
    """Wait, bounded, until the announced first start has finished; False when it has not."""
    deadline = time.monotonic() + timeout
    while service_start_pending():
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.02 if timeout < 1 else 0.25)
    return True


def get_analysis_health() -> dict:
    return runtime.health() if runtime else {"state": "unavailable", "reason": "not_started"}


async def start_analysis_runtime(base: Path):
    global runtime, _started_once
    runtime = AnalysisRuntime(base, get_local_worker_broker())
    try:
        await runtime.start()
    finally:
        _started_once = True


async def stop_analysis_runtime():
    global runtime, _start_expected
    _start_expected = False  # a stopping process waits for nothing
    if runtime:
        await runtime.stop()
        runtime = None


async def analyze_3mf(path: Path, plate_id: int | None):
    from backend.app.services.print_file_analysis import AnalysisResourceError

    if runtime is None:
        raise AnalysisResourceError("analysis service unavailable")
    return await runtime.parse(path, plate_id)
