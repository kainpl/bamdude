"""Main's side of the assembly microservice: start and stop it, hand it a staged job, log what became of it.

The packing itself never runs here and main never watches the packing child: the service owns the queue and
the child (``backend/app/assembly_service.py``). A caller stages a directory under :attr:`AssemblyRuntime.staging`,
calls :func:`run_assembly` and reads the returned result; it removes its directory itself.

The parent log is this module's: service readiness, loss and restart, and for every job one start line and
exactly one outcome line with the elapsed time. No user file path and no child output is ever logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from backend.app.services.local_worker_broker import get_local_worker_broker
from backend.app.services.preview_artifacts import disk, owned
from backend.app.services.preview_process import PreviewProcess
from backend.app.services.preview_protocol import CONTROL_SECONDS, PreviewError, closed_why, decode, encode

logger = logging.getLogger(__name__)

OUTCOMES = ("failed", "timeout", "canceled", "resource_limit", "busy", "unavailable")
RESULT_BYTES = 256 * 1024
_POLL_SECONDS = 1.0
_BOOTSTRAP_ATTEMPTS = 3
_BOOTSTRAP_REQUEST_SECONDS = 10
_BOOTSTRAP_RETRY_SECONDS = 0.5
_LAUNCH_SECONDS = 20


class AssemblyFailed(Exception):
    """A job that did not produce a result. ``outcome`` is one of :data:`OUTCOMES`; ``why`` is a closed word for
    the log and for the caller's own sentence; ``parts`` names the source parts a failed job blamed."""

    def __init__(self, outcome: str, why: str = "", parts: list | None = None):
        super().__init__(f"{outcome}: {why}" if why else outcome)
        self.outcome = outcome if outcome in OUTCOMES else "failed"
        self.why = closed_why(why) or ""
        self.parts = parts or []


def _read_result(root: Path) -> dict | None:
    try:
        path = root / "result.json"
        if path.stat().st_size > RESULT_BYTES:
            return None
        data = json.loads(path.read_bytes())
    except (OSError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


class AssemblyRuntime:
    def __init__(self, base: Path, broker):
        self.base = base
        self.root = base / ".cache" / "assembly-service"
        self.generation = uuid.uuid4().hex
        self.staging = self.root / "staging" / self.generation
        self.broker = broker
        self.epoch = uuid.uuid4().hex
        self.nc = self.service = self.monitor = None
        self.startup = self._stop_task = None
        self.active: set[asyncio.Task] = set()
        self.interrupted: set[asyncio.Task] = set()
        self.ready = self.closed = self.uncertain = False
        self.reason: str | None = "not_started"
        self.restarts: list[float] = []
        self.circuit_until = 0.0
        self.stats = {"jobs": 0, "ok": 0, "failed": 0, "canceled": 0, "service_restart_attempts": 0}

    def health(self) -> dict:
        return {
            "state": "ready" if self.ready and not self.closed else "unavailable",
            "reason": self.reason,
            "jobs": dict(self.stats),
        }

    def quiesced(self) -> bool:
        """Nothing of a job is left running here and every child's end was proven."""
        return not self.active and not self.uncertain

    # --- lifecycle ---------------------------------------------------------------------------------------------

    async def start(self) -> None:
        if self.closed:
            return
        if self.startup is None:
            self.startup = asyncio.create_task(self._start(), name="assembly-service-startup")
        try:
            await asyncio.shield(self.startup)
        except asyncio.CancelledError:
            await owned(self.stop())
            raise
        if not self.ready and self.monitor is None:
            await self.stop()

    async def _start(self) -> None:
        self.reason = "starting"
        started = time.monotonic()
        try:
            if self.broker is None:
                raise RuntimeError("local broker unavailable")
            await disk(self._report_retained_staging)
            await disk(self.staging.mkdir, parents=True, exist_ok=True, mode=0o700)
            await self._connect()
            await self.launch()
            logger.info(
                "Assembly service ready: generation=%s elapsed_ms=%.1f",
                self.generation,
                (time.monotonic() - started) * 1000,
            )
            self.monitor = asyncio.create_task(self._monitor(), name="assembly-service-monitor")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.reason = "startup_failed"
            logger.warning(
                "Assembly service unavailable: generation=%s error=%s elapsed_ms=%.1f",
                self.generation,
                type(exc).__name__,
                (time.monotonic() - started) * 1000,
            )
            if self.nc and self.nc.is_connected:
                # a service that failed to come up once is retried by the monitor, not left dead until a restart
                try:
                    await self.retire()
                except Exception:
                    pass
                if not self.uncertain:
                    self.monitor = asyncio.create_task(self._monitor(), name="assembly-service-monitor")

    def _report_retained_staging(self) -> None:
        """Staging a dead run left behind is reported, never removed: its owner is not proven gone."""
        parent = self.staging.parent
        try:
            left = [path.name for path in parent.iterdir() if path != self.staging]
        except OSError:
            return
        if left:
            logger.warning(
                "Retained assembly staging: count=%d under %s; verify owners before manual cleanup", len(left), parent
            )

    async def _connect(self) -> None:
        from nats.aio.client import Client
        from nats.errors import ConnectionClosedError, NoServersError, StaleConnectionError

        for attempt in range(1, _BOOTSTRAP_ATTEMPTS + 1):
            client = self.nc = Client()  # retained before connect: shutdown owns a handshake that has not returned

            async def disconnected(client=client):
                if self.nc is client:
                    await self.disconnected()

            async def error(exc):
                logger.debug("Assembly transport error: error=%s", type(exc).__name__)

            try:
                await client.connect(
                    self.broker.url,
                    token=self.broker.token,
                    allow_reconnect=False,
                    connect_timeout=_BOOTSTRAP_REQUEST_SECONDS,
                    closed_cb=disconnected,
                    error_cb=error,
                )
                return
            except (OSError, TimeoutError, ConnectionClosedError, NoServersError, StaleConnectionError) as exc:
                await self._close_transport()
                if attempt == _BOOTSTRAP_ATTEMPTS:
                    raise
                logger.warning(
                    "Assembly service startup_retry: generation=%s attempt=%d error=%s",
                    self.generation,
                    attempt,
                    type(exc).__name__,
                )
                await asyncio.sleep(_BOOTSTRAP_RETRY_SECONDS * attempt)

    async def _close_transport(self) -> None:
        client, self.nc = self.nc, None  # detached first: our own close is not a broker failure
        if client is not None:
            await owned(client.close())

    def command(self, operation: str, attempt: str | None = None, *, root: Path | None = None, budget_ns=None) -> dict:
        return {
            "version": 1,
            "generation": self.generation,
            "epoch": self.epoch,
            "attempt_id": attempt or uuid.uuid4().hex,
            "operation": operation,
            "root": str(root) if root is not None else None,
            "budget_ns": budget_ns,
        }

    async def rpc(self, command: dict, timeout: float = CONTROL_SECONDS) -> dict:
        message = await self.nc.request(
            f"bamdude.assembly.{self.generation}.{self.epoch}", encode(command), timeout=timeout
        )
        return decode(message.data)

    async def launch(self) -> None:
        self.epoch = uuid.uuid4().hex

        async def spawn():
            self.service = await disk(
                PreviewProcess,
                "backend.app.assembly_service",
                {
                    "url": self.broker.url,
                    "token": self.broker.token,
                    "generation": self.generation,
                    "epoch": self.epoch,
                    "staging": str(self.staging),
                    "cache": str(self.root / "cache"),
                },
                self.root / "cache",
            )

        await owned(spawn())
        deadline = time.monotonic() + _LAUNCH_SECONDS
        while time.monotonic() < deadline:
            if self.service.process.poll() is not None:
                raise PreviewError("unavailable")
            try:
                before = time.monotonic_ns()
                reply = await self.rpc(self.command("ready"))
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

    async def disconnected(self) -> None:
        if self.closed:
            return
        self.ready = False
        self.reason = "broker_disconnected"
        self._interrupt()

    def _lost(self, why: str) -> None:
        """Said once per loss, by whoever notices first: the monitor or a job whose request went unanswered."""
        if self.ready:
            logger.warning("Assembly service lost: generation=%s epoch=%s why=%s", self.generation, self.epoch, why)
        self.ready = False

    def _interrupt(self) -> None:
        for task in self.active - self.interrupted:
            self.interrupted.add(task)
            task.cancel()

    async def retire(self) -> None:
        """Stop the service and everything under it. Raises, and marks the runtime uncertain, without proof."""
        self.ready = False
        service = self.service
        if service is None:
            return
        try:
            await disk(service.stop)
        except Exception:
            self.uncertain = True
            self.reason = "ownership_uncertain"
            raise
        self.service = None

    async def _monitor(self) -> None:
        while not self.closed:
            await asyncio.sleep(0.5)
            if self.service and self.service.process.poll() is None and self.nc.is_connected:
                continue
            self._lost("process_or_broker")
            if self.active:
                self._interrupt()
                continue
            if self.uncertain:
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
                logger.info("Assembly service restarted: generation=%s epoch=%s", self.generation, self.epoch)
            except Exception as exc:
                logger.warning("Assembly service restart failed: %s", type(exc).__name__)
                try:
                    await self.retire()
                except Exception:
                    return

    # --- one job -----------------------------------------------------------------------------------------------

    async def _cancel(self, attempt: str) -> None:
        """End the attempt in the service. Without its acknowledgement the whole service is retired instead."""
        if self.nc and self.nc.is_connected and self.service and self.service.process.poll() is None:
            try:
                # the answer comes only after the child is proven gone, which a stop may take seconds to prove
                reply = await self.rpc(self.command("cancel", attempt), timeout=30)
                if reply.get("outcome") == "canceled":
                    return
            except Exception:
                pass
        await self.retire()

    async def run(
        self,
        root: Path,
        *,
        job_id: int,
        timeout_s: float = 900.0,
        on_progress: Callable[[dict], None] | None = None,
    ) -> dict:
        """Pack the job staged in ``root`` and return the child's result. Raises :class:`AssemblyFailed`."""
        if not self.ready or self.closed:
            raise AssemblyFailed("unavailable", "not_ready")
        attempt = uuid.uuid4().hex
        task = asyncio.current_task()
        self.active.add(task)
        self.stats["jobs"] += 1
        started = time.monotonic()
        outcome, why = "failed", "unknown"
        accepted = finished = False
        logger.info("Assembly job=%s attempt=%s start", job_id, attempt)
        try:
            reply = await self.rpc(self.command("run", attempt, root=root, budget_ns=int(timeout_s * 1e9)))
            if reply.get("outcome") != "ok":
                refused = reply.get("outcome")
                raise AssemblyFailed(refused if refused == "busy" else "unavailable", "refused")
            accepted = True
            shown = None
            while True:
                state = reply.get("state")
                if state == "done":
                    break
                if state == "queued":
                    progress = {"stage": "waiting", "position": reply.get("position")}
                elif state == "running":
                    progress = {"stage": "packing", **(reply.get("progress") or {})}
                else:
                    raise AssemblyFailed("unavailable", "job_lost")
                if on_progress is not None and progress != shown:
                    shown = progress
                    on_progress(dict(progress))
                await asyncio.sleep(_POLL_SECONDS)
                reply = await self.rpc(self.command("status", attempt))
                if reply.get("outcome") != "ok":
                    raise AssemblyFailed("unavailable", "status_refused")
            finished = True
            verdict = reply.get("result") if isinstance(reply.get("result"), dict) else {}
            outcome, why = verdict.get("outcome"), closed_why(verdict.get("why")) or ""
            result = await disk(_read_result, root) if outcome in ("ok", "failed") else None
            if outcome == "ok":
                if result is None or result.get("outcome") != "ok":
                    raise AssemblyFailed("failed", "bad_result")
                self.stats["ok"] += 1
                return result
            if outcome == "unavailable":
                self._lost("ownership_uncertain")
                try:
                    await owned(self.retire())  # the service could not prove a child gone: its whole tree goes
                except Exception:
                    pass
            parts = result.get("parts") if result and isinstance(result.get("parts"), list) else None
            raise AssemblyFailed(outcome if outcome in OUTCOMES else "failed", why, parts)
        except AssemblyFailed as failed:
            outcome, why = failed.outcome, failed.why
            self.stats["failed"] += 1
            if accepted and not finished:
                await self._abandon(attempt)
            raise
        except asyncio.CancelledError:
            lost = task in self.interrupted
            outcome, why = ("unavailable", "service_lost") if lost else ("canceled", "cancel")
            self.stats["failed" if lost else "canceled"] += 1
            if not finished:
                await self._abandon(attempt)
            if lost:
                raise AssemblyFailed("unavailable", "service_lost") from None
            raise
        except Exception as exc:  # the transport: a timeout, no responders, a closed connection
            outcome, why = "unavailable", closed_why(type(exc).__name__) or "transport"
            self.stats["failed"] += 1
            self._lost(why)
            await self._abandon(attempt)
            raise AssemblyFailed("unavailable", why) from exc
        finally:
            self.active.discard(task)
            self.interrupted.discard(task)
            logger.info(
                "Assembly job=%s attempt=%s outcome=%s why=%s elapsed_ms=%d",
                job_id,
                attempt,
                outcome,
                why,
                (time.monotonic() - started) * 1000,
            )

    async def _abandon(self, attempt: str) -> None:
        try:
            await owned(self._cancel(attempt))
        except Exception:
            self.uncertain = True
            self.reason = "ownership_uncertain"

    async def stop(self) -> None:
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop(), name="assembly-service-stop")
        await owned(self._stop_task)

    async def _stop(self) -> None:
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
        jobs = [task for task in self.active if task is not asyncio.current_task()]
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        try:
            await self.retire()
        except Exception:
            logger.exception("Assembly service ownership uncertain; staging retained")
        await self._close_transport()
        if not self.uncertain:
            from backend.app.services.worker_staging import cleanup_owned

            result = await disk(cleanup_owned, self.staging)
            if result.status == "retained_error":
                logger.warning(
                    "Assembly staging_cleanup_failed: error=%s code=%s", result.error_type, result.error_code
                )


runtime: AssemblyRuntime | None = None


def get_assembly_health() -> dict:
    return runtime.health() if runtime else {"state": "unavailable", "reason": "not_started"}


def assembly_staging() -> Path | None:
    """Where a caller stages a job's directory; None while the service is not running."""
    return runtime.staging if runtime and runtime.ready and not runtime.closed else None


def assembly_quiesced() -> bool:
    return runtime is None or runtime.quiesced()


async def start_assembly_runtime(base: Path) -> None:
    global runtime
    runtime = AssemblyRuntime(base, get_local_worker_broker())
    await runtime.start()


async def stop_assembly_runtime() -> None:
    global runtime
    if runtime:
        await runtime.stop()
        runtime = None


async def run_assembly(root: Path, *, job_id: int, timeout_s: float = 900.0, on_progress=None) -> dict:
    if runtime is None:
        raise AssemblyFailed("unavailable", "not_started")
    return await runtime.run(root, job_id=job_id, timeout_s=timeout_s, on_progress=on_progress)
