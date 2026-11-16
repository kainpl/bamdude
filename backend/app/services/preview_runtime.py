"""Lifespan-owned, local-only broker/service and bounded preview admission."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from backend.app.schemas.system import PreviewHealth
from backend.app.services.preview_arbiter import Arbiter
from backend.app.services.preview_artifacts import (
    Revoked,
    close_transfer,
    describe,
    disk,
    get,
    owned,
    put,
    snapshot,
)
from backend.app.services.preview_process import PreviewProcess, SpawnUnproven
from backend.app.services.preview_protocol import (
    ACTIVE_SECONDS,
    ATTEMPT_BYTES,
    BUCKET_BYTES,
    C_CANCEL_SECONDS,
    CAD_KINDS,
    CONTROL_SECONDS,
    KINDS,
    OUTCOMES,
    PREVIEW_OWNER,
    QUARANTINE_ATTEMPTS,
    QUEUE_LIMIT,
    QUEUE_SECONDS,
    STAGES,
    STAGING_BYTES,
    STARTUP_SECONDS,
    TTL_SECONDS,
    VERSION,
    Artifact,
    Command,
    PreviewError,
    decode,
    encode,
    proves_end,
    remaining,
    reply_diagnostics,
)
from backend.app.services.worker_staging import (
    abandoned_attempts,
    cleanup_owned,
    quarantine_owned,
    quarantine_usage,
    reclaim_quarantine,
    retained_entries,
)

logger = logging.getLogger(__name__)
RECOVERY_GUIDE = "https://docs.bamdude.top/reference/troubleshooting/#local-preview-service"
# A sliced (high) waiter may have to wait for an active STEP's revoke, cancel proof and cleanup: its own finite
# budget is C_cancel plus the normal admission wait, counted once (never extended per poll). Spec §4.2.
HIGH_WAIT_SECONDS = C_CANCEL_SECONDS + QUEUE_SECONDS
# A PDF's first page is a quick render: an ordinary attempt, never queued behind a STEP conversion.
PRIORITY_BY_KIND = {"sliced": "high", "mesh": "normal", "pdf": "normal", "step": "low"}
# A first start that fails with a PROVEN cleanup is tried again in the background (spec
# preview-runtime-first-start-recovery r2): attempts in all, the pauses between them, and how long the service
# may take to answer ready inside one attempt.
STARTUP_ATTEMPTS = 3
STARTUP_PAUSES = (2.0, 4.0)
READY_SECONDS = 8.0


@dataclass
class PreviewResult:
    outcome: str = "unavailable"
    files: dict[str, Path] = field(default_factory=dict)
    input_digests: dict[str, str] = field(default_factory=dict)
    attempt_id: str | None = None
    kind: str | None = None  # "sliced" | "pdf" | "step" | "mesh", from the input ROLES
    stage: str | None = None  # the last protocol stage a STEP attempt reached (bounded enum, for the parent log)
    # The service's closed diagnostics of a failure, for the parent log only — nothing decides on them: a reason
    # token or an exception's class (never text), who decided (a child by its operation, or the service), and
    # that child's exit code.
    why: str | None = None
    by: str | None = None
    exit_code: int | None = None
    # A publication lease was taken at the hand-off: the ONLY right to attach a library picture. A usable result
    # (ok, or a timeout's checkpoint) without it publishes nothing (Codex A3-R1).
    publishable: bool = False


class PublicationDeferred(Revoked):
    """A backup is copying the library's pictures: a result that must be published cannot be now. The INTERNAL
    outcome ``publication_deferred`` — nothing handed off, nothing kept, the attempt's end proven and the slot freed;
    its owner retries after the pause. Never a failure and never a preemption (Codex A-r3, the owner's (b))."""


def _inputs_kind(inputs: dict) -> str:
    if "sliced" in inputs:
        return "sliced"
    if "document" in inputs:
        return "pdf"
    mesh = inputs.get("mesh")
    return "step" if mesh and mesh[1] in CAD_KINDS else "mesh"


class PreviewRuntime:
    def __init__(self, root: Path, shared_broker=None, *, app_dir: Path | None = None):
        self.root = root
        self.shared_broker = shared_broker
        # where the STEP bundle lives (<app_dir>/runtime/step-preview), the same root as the part-render Node
        self.app_dir = Path(app_dir) if app_dir else Path(__file__).resolve().parents[3]
        # proven-dead attempts whose staging AV or a lock kept: beside the runtime, never inside active staging
        self.quarantine = self.root.parent / "preview-quarantine"
        # proven-dead attempt dirs whose cleanup AND move failed: reclaimed again at every admission (spec §4.2)
        self._dead_residue: dict[Path, str] = {}
        self.generation = uuid.uuid4().hex
        self.epoch = uuid.uuid4().hex
        self.bucket = f"bamdude_preview_{self.generation}"
        self.broker = self.nc = self.service = self.store = None
        self.ready = False
        self.closed = False
        self.uncertain = False
        self.sequence = 0
        self.waiters = 0
        # one slot; high > normal > low; only high revokes an active low, whose time never spends an STL's wait
        self.slot = Arbiter(low_grace=C_CANCEL_SECONDS)
        self.active_task = None
        self.result_handed_off = False
        self.monitor = None
        self.restarts = []
        self.circuit_until = 0.0
        self.dependency_lost = False
        self.reason = "not_started"
        self.error_type = None
        self._last_staging_issue = None
        self._last_admission_issue = None
        self.starter: asyncio.Task | None = None  # the one owner of the start, its first attempt included
        self.start_attempt = 0
        self.phase: str | None = None  # the start phase last entered (spec R1)
        self._phase_started = 0.0
        self._failed_phase: str | None = None
        self._failed_phase_ms = 0.0
        self._start_begun = 0.0
        self._start_outcome_logged = False  # ready, a final failure or a stop: said once (FSR-I-M1)
        self._stop_task: asyncio.Task | None = None  # the ONE stop of this runtime, shared by every caller

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
        """Start this runtime. The first attempt runs inline and this returns once it has ended (ready, or failed);
        when it failed with a PROVEN cleanup, up to ``STARTUP_ATTEMPTS`` - 1 more follow in the background. One
        owner holds all of it (``self.starter``): a stop cancels it and waits for its attempt and that attempt's
        cleanup (spec preview-runtime-first-start-recovery r2, R4–R5)."""
        if self.starter is not None:
            raise RuntimeError("this preview runtime was already started")
        self.reason = "starting"
        self._start_begun = time.monotonic()
        first_ended = asyncio.Event()
        self.starter = asyncio.create_task(self._run_start(first_ended), name="preview-runtime-start")
        self.starter.add_done_callback(self._starter_ended)
        verdict = asyncio.ensure_future(first_ended.wait())
        try:
            # The first verdict — or the owner's end: a stop that lands before the owner's first step leaves no
            # verdict at all (FSR-I-R2).
            await asyncio.wait({verdict, self.starter}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            # The caller gave up on the start (an aborted lifespan, a cancelled test): the owner must not outlive
            # it and launch a service nobody stops. The runtime's ONE stop ends it — owned, so a repeated cancel
            # cannot cut its cleanup short — and only then does the cancellation go on (FSR-I-R1).
            await owned(self.stop())
            raise
        finally:
            verdict.cancel()

    def _starter_ended(self, task: asyncio.Task) -> None:
        if task.cancelled() and not self._start_outcome_logged:
            self._log_start_stopped()  # also a stop before the owner's first step: it never ran its own code

    def _log_start_stopped(self) -> None:
        self._start_outcome_logged = True
        logger.info(
            "Preview startup stopped: generation=%s attempt=%d/%d phase=%s elapsed_ms=%d; its cleanup is the stop's",
            self.generation,
            self.start_attempt,
            STARTUP_ATTEMPTS,
            self.phase or "none",
            (time.monotonic() - self._start_begun) * 1000,
        )

    async def _run_start(self, first_ended: asyncio.Event) -> None:
        begun = time.monotonic()
        # Admission of a NEW attempt ends here, counted from the first attempt: three budgets and two pauses. It
        # never cuts a cleanup short — a cleanup that ran past it admits no further spawn (R4).
        admit_until = begun + STARTUP_ATTEMPTS * STARTUP_SECONDS + sum(STARTUP_PAUSES)
        try:
            for attempt in range(1, STARTUP_ATTEMPTS + 1):
                self.start_attempt = attempt
                started = time.monotonic()
                verdict, exc = await self._start_attempt(attempt)
                first_ended.set()
                if verdict == "ready":
                    self._start_outcome_logged = True
                    logger.info(
                        "Preview startup ready: generation=%s attempts=%d elapsed_ms=%d",
                        self.generation,
                        attempt,
                        (time.monotonic() - begun) * 1000,
                    )
                    return
                if verdict == "final" and exc is None:
                    self._log_start_stopped()  # a stop came in as the last step returned: it owns what is left
                    return
                if verdict == "final":
                    next_step = "final"
                elif attempt == STARTUP_ATTEMPTS:
                    next_step = "exhausted_attempts"
                else:
                    pause = STARTUP_PAUSES[min(attempt - 1, len(STARTUP_PAUSES) - 1)]
                    next_step = (
                        "exhausted_deadline" if time.monotonic() + pause >= admit_until else f"retry_in_{pause:g}s"
                    )
                self._log_start_failure(attempt, exc, started, begun, next_step)
                if not next_step.startswith("retry_in_"):
                    await self._give_up(exc)
                    return
                await asyncio.sleep(pause)  # a stop cancels it at once
                if self.closed:
                    return
                if time.monotonic() >= admit_until:
                    # Asked again on waking: a late wake — a suspended process, a busy loop — admits nothing past
                    # the deadline (FSR-I-R3). The pause is not repeated and the budget is not renewed.
                    self._log_start_failure(attempt, exc, started, begun, "exhausted_deadline")
                    await self._give_up(exc)
                    return
        finally:
            first_ended.set()

    async def _give_up(self, exc) -> None:
        """The owner's own end of a start that failed for good. A stop already under way owns the rest."""
        if self.closed:
            return
        if self.reason not in {"recovery_required", "ownership_uncertain"}:
            self._unavailable("startup_failed", exc)
        await self.stop(from_owner=True)

    def _log_start_failure(self, attempt: int, exc, started: float, begun: float, next_step: str) -> None:
        if not next_step.startswith("retry_in_"):
            self._start_outcome_logged = True
        now = time.monotonic()
        logger.warning(
            "Preview startup_failed: generation=%s attempt=%d/%d phase=%s error=%s phase_ms=%d elapsed_ms=%d next=%s",
            self.generation,
            attempt,
            STARTUP_ATTEMPTS,
            self._failed_phase,
            type(exc).__name__ if exc is not None else "none",
            self._failed_phase_ms,
            (now - begun) * 1000,
            next_step,
        )

    def _enter(self, phase: str, attempt: int | None) -> None:
        """Entering a start phase is logged — not only leaving it: an owned I/O that hangs never leaves (R1)."""
        self.phase, self._phase_started = phase, time.monotonic()
        if attempt is not None:
            logger.info(
                "Preview startup phase=%s generation=%s attempt=%d/%d",
                phase,
                self.generation,
                attempt,
                STARTUP_ATTEMPTS,
            )

    async def _start_attempt(self, attempt: int):
        """One attempt. ``("ready", None)``; ``("retry", exc)`` only when its own cleanup PROVED everything it may
        have started gone (R2); ``("final", exc)`` otherwise. A stop's cancellation is not caught here."""
        recovery_errors: tuple[type[Exception], ...] = ()
        if attempt > 1:
            self.reason = "worker_restarting"
        self.phase = None  # a failure before any phase (a missing dependency) is final: a retry repeats it
        try:
            from embedded_nats import NatsServer, RecoveryRequired

            recovery_errors = (RecoveryRequired,)
            if attempt == 1:
                self._enter("staging", attempt)
                self.token = self.shared_broker.token if self.shared_broker else secrets.token_urlsafe(32)
                await disk(self._directories)
                self.broker = self.shared_broker or NatsServer(
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
                if attempt == 1 and not self.shared_broker:
                    self._enter("broker", attempt)
                    await disk(self.broker.start)
                    if self.broker.recovered_generation:
                        logger.warning(
                            "Preview broker recovered abandoned generation=%s; runtime_dir=%s. "
                            "Old preview staging is retained until manual ownership verification; see %s",
                            self.broker.recovered_generation,
                            self.root,
                            RECOVERY_GUIDE,
                        )
                await self._bootstrap_transport(attempt)
                await self._launch(attempt)
            if self.closed:
                return "final", None  # a stop came in as the last step returned: it owns what is left
            self.monitor = asyncio.create_task(self._monitor(), name="preview-service-monitor")
            return "ready", None
        except Exception as exc:
            # the phase it failed in and how long it had run, read BEFORE the cleanup phase is entered
            self._failed_phase, self._failed_phase_ms = self.phase, (time.monotonic() - self._phase_started) * 1000
            if isinstance(exc, recovery_errors):
                self._unavailable("recovery_required", exc)
                logger.warning(
                    "Preview broker recovery refused: reason=%s marker=%s; follow %s",
                    exc.reason,
                    exc.marker_path or (self.broker.recovery_marker_path if self.broker else None),
                    RECOVERY_GUIDE,
                )
                return "final", exc
            if self.uncertain or self.closed or self._failed_phase in {None, "staging", "broker"}:
                return "final", exc  # unproven ownership, a stop under way, or the broker/staging itself failed
            proven = await self._abandon_attempt(attempt)
            return ("retry" if proven else "final"), exc

    async def _bootstrap_transport(self, attempt: int) -> None:
        """This attempt's client, owned BEFORE connect — a handshake that times out or is cancelled still has an
        owner that closes it (Codex FSR-R2) — and bound by identity: a late callback of an earlier attempt's
        client changes nothing here."""
        from nats.aio.client import Client
        from nats.js.api import ObjectStoreConfig

        client = self.nc = Client()

        async def closed(client=client):
            if self.nc is client:
                await self.disconnected()

        async def error(exc):
            logger.debug("Preview transport error: generation=%s error=%s", self.generation, type(exc).__name__)

        self._enter("connect", attempt)
        await client.connect(
            self.broker.url,
            token=self.token,
            allow_reconnect=False,
            connect_timeout=CONTROL_SECONDS,
            closed_cb=closed,
            error_cb=error,
        )
        js = client.jetstream(timeout=CONTROL_SECONDS)
        # A clean package start proves exclusive store ownership. Old preview buckets — this generation's own
        # from an earlier attempt among them — are disposable; no other namespace is touched.
        self._enter("stream_list", attempt)
        for stream in await js.streams_info():
            if stream.config.name.startswith("OBJ_bamdude_preview_"):
                self._enter("stream_delete", attempt)
                await js.delete_stream(stream.config.name)
        if attempt == 1:
            # Broker ownership says nothing about an old worker/renderer's disk I/O. Even a later clean restart
            # cannot prove it is gone.
            self._enter("staging", attempt)
            await disk(self._report_retained_staging)
        self._enter("store_create", attempt)
        self.store = await js.create_object_store(
            bucket=self.bucket,
            config=ObjectStoreConfig(bucket=self.bucket, max_bytes=BUCKET_BYTES, ttl=TTL_SECONDS, storage="file"),
        )

    async def _close_transport(self) -> bool:
        """Detach first — an intentional close is no broker failure — then close, owned. False when the close
        itself failed: no new attempt follows a transport whose end is not known (R2)."""
        client, self.nc = self.nc, None
        self.store = None
        if client is None:
            return True
        try:
            await owned(client.close())
        except Exception as exc:
            logger.warning(
                "Preview transport close failed: generation=%s error=%s", self.generation, type(exc).__name__
            )
            return False
        return True

    async def _abandon_attempt(self, attempt: int) -> bool:
        """The failed attempt's own cleanup, outside its budget and never cut short: its service stopped with proof
        (or never created), its client closed and detached, the slot free. True only with all of it (R2)."""
        self._enter("cleanup", attempt)
        try:
            await self._retire()
        except Exception:
            return False  # _retire has made the runtime ownership_uncertain
        closed = await self._close_transport()
        return closed and not self.uncertain and not self.slot.locked()

    def _directories(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.staging = self.root / "staging" / self.generation
        self.staging.mkdir(parents=True, mode=0o700)
        (self.staging / "main").mkdir(mode=0o700)
        (self.staging / "service").mkdir(mode=0o700)

    def _report_retained_staging(self):
        empty = 0
        for directory, classification, reason in retained_entries(self.staging.parent, self.staging, "preview"):
            if classification == "empty_skeleton":
                empty += 1
                logger.debug("Preview staging skeleton retained: path=%s", directory)
            else:
                logger.warning(
                    "Retained preview staging: path=%s classification=%s reason=%s; "
                    "old worker/renderer ownership is unproven; verify owners before manual cleanup; see %s",
                    directory,
                    classification,
                    reason,
                    RECOVERY_GUIDE,
                )
        if empty:
            logger.info("Preview staging empty_skeleton_count=%d; no manual cleanup required", empty)

    def _cleanup_result(self, result, phase: str, attempt: str | None = None):
        if result.status == "retained_error":
            issue = (phase, str(result.path), result.error_type, result.error_code)
            log = logger.warning if issue != self._last_staging_issue else logger.debug
            log(
                "Preview staging_cleanup_failed: phase=%s attempt=%s path=%s error=%s code=%s",
                phase,
                attempt,
                result.path,
                result.error_type,
                result.error_code,
            )
            self._last_staging_issue = issue
        else:
            self._last_staging_issue = None

    async def _quarantine(self, result, path: Path, side: str):
        """After the bounded reclaim failed: move the proven-dead owned attempt dir into quarantine (spec §4.2).
        A refused or failed move keeps the original retained result — the residue stays and blocks admission."""
        moved = await disk(quarantine_owned, path, self.quarantine, self.generation, side)
        if moved.status == "quarantined":
            logger.warning(
                "Preview staging_quarantined: side=%s attempt=%s; counted against the staging limits until reclaimed",
                side,
                path.name,
            )
            return moved
        logger.warning(
            "Preview staging_quarantine_failed: side=%s attempt=%s status=%s error=%s",
            side,
            path.name,
            moved.status,
            moved.error_type,
        )
        return result

    async def _reclaim_dead_residue(self) -> None:
        """Retry, bounded, every proven-dead attempt dir that could neither be removed nor moved — one AV lock must
        not disable previews until a restart. Removed or quarantined entries are forgotten; the rest stay and the
        residue check still refuses admission."""
        for path, side in list(self._dead_residue.items()):
            result = await disk(cleanup_owned, path, deadline=time.monotonic() + 1.0)
            if result.status == "retained_error":
                result = await self._quarantine(result, path, side)
            if result.status in {"removed", "absent", "quarantined"}:
                self._dead_residue.pop(path, None)
                if result.status == "removed":
                    logger.info("Preview staging_residue_reclaimed: side=%s attempt=%s", side, path.name)

    def _service_cleanup_reply(self, reply, attempt_id):
        detail = reply.get("staging_cleanup") if isinstance(reply, dict) else None
        staging = getattr(self, "staging", None)
        owned = (
            isinstance(attempt_id, str) and len(attempt_id) == 32 and all(c in "0123456789abcdef" for c in attempt_id)
        )
        if isinstance(detail, dict) and not detail.get("quarantined") and staging is not None and owned:
            # the service's run is over (it replied after its finally): its attempt dir is dead. The path is rebuilt
            # from the attempt id under this generation's service staging — the reply's own path is only logged.
            self._dead_residue[staging / "service" / attempt_id] = "service"
        if isinstance(detail, dict):
            logger.warning(
                "Preview staging_cleanup_failed: phase=service_attempt attempt=%s path=%s error=%s code=%s",
                attempt_id,
                detail.get("path"),
                detail.get("error"),
                detail.get("code"),
            )

    def _command(self, operation="ready", manifest=(), attempt_id=None, deadline=None, cad=None):
        self.sequence += 1
        return Command(
            VERSION,
            self.generation,
            self.epoch,
            attempt_id or uuid.uuid4().hex,
            self.sequence,
            operation,
            deadline or time.monotonic_ns() + int(CONTROL_SECONDS * 1e9),
            tuple(manifest),
            cad,
        )

    async def _launch(self, attempt: int | None = None):
        self.epoch = uuid.uuid4().hex

        async def spawn():
            try:
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
                        # the root the service rebuilds a STEP bundle path under, from the command's key/digest only
                        "app_dir": str(self.app_dir),
                        "quarantine": str(self.quarantine),
                    },
                    self.root / "matplotlib",
                )
            except SpawnUnproven as exc:
                # Recorded HERE, inside the owned task: an outer timeout or stop that landed meanwhile only reads
                # this task's result and raises its own error, which would hide it (Codex FSR-R1). No service was
                # assigned, and that is no proof that nothing runs: final until BamDude restarts.
                self.uncertain = True
                self._unavailable("ownership_uncertain", exc)
                logger.warning(
                    "Preview service spawn unproven: generation=%s guardian_pid=%s; restart disabled until BamDude "
                    "restarts",
                    self.generation,
                    exc.pid,
                )
                raise

        # Capture ownership even when cancellation lands during Popen/Job attach.
        self._enter("service_spawn", attempt)
        await owned(spawn())
        self._enter("service_ready", attempt)
        deadline = time.monotonic() + READY_SECONDS
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
                    and reply.get("protocol_version") == VERSION
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
            if not self.result_handed_off:
                self.active_task.cancel()

    async def _retire(self):
        self.ready = False
        if not self.closed and not self.uncertain and self.reason != "broker_disconnected":
            self.reason = "worker_restarting"
        if self.service:
            retired_epoch = self.epoch
            try:
                await disk(self.service.stop)
            except Exception:
                self.uncertain = True
                self._unavailable("ownership_uncertain")
                raise
            self.service = None
            service_root = self.staging / "service"
            attempts, unknown = await disk(abandoned_attempts, service_root)
            deadline = time.monotonic() + 2.0
            for path in unknown:
                logger.warning(
                    "Preview service residue unknown: generation=%s epoch=%s path=%s",
                    self.generation,
                    retired_epoch,
                    path,
                )
            for path in attempts:
                result = await disk(cleanup_owned, path, deadline=deadline)
                logger.info(
                    "Preview service_residue_after_retire: generation=%s epoch=%s path=%s result=%s",
                    self.generation,
                    retired_epoch,
                    path,
                    result.status,
                )
                self._cleanup_result(result, "service_residue_after_retire", path.name)
                if result.status == "retained_error":  # the service is proven stopped: its attempt may move
                    await self._quarantine(result, path, "service")

    async def _cancel(self, command) -> str | None:
        # Revoke in the caller first. Only a proof of END is accepted (spec §6.1): a cancel_ack after the service
        # reaped its child and joined owned I/O, or a terminal_replay of exactly this command after its run's
        # finally. A timeout the service already reached is such an end — no retirement for it. Anything else
        # (bare outcome, busy, unavailable, an uncertain service, a forged field, no reply in C_cancel) retires.
        # Returns the stage the service's answer names: a run that never replied still says where it stopped.
        stage = None
        if command and self.nc is not None and self.nc.is_connected:
            try:
                reply = await self._rpc(command, "cancel", timeout=C_CANCEL_SECONDS)
                self._service_cleanup_reply(reply, command.attempt_id)
                if isinstance(reply, dict) and reply.get("stage") in STAGES:
                    stage = reply["stage"]
                if proves_end(command, reply):
                    return stage
            except Exception:
                pass
        await self._retire()
        return stage

    async def _monitor(self):
        while not self.closed:
            await asyncio.sleep(0.5)
            if self.service and self.service.process.poll() is None and self.nc is not None and self.nc.is_connected:
                continue
            self.ready = False
            if self.active_task:
                self.dependency_lost = True
                if not self.result_handed_off:
                    self.active_task.cancel()
                continue
            if self.slot.locked() or self.uncertain:
                continue
            try:
                await self._retire()
            except Exception:
                logger.warning("Preview process ownership uncertain; restart disabled")
                return
            if self.nc is None or not self.nc.is_connected:  # Broker recovery: the next application start.
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
                if self.uncertain:  # a relaunch whose spawn cleanup was unproven is final too (R2)
                    logger.warning("Preview process ownership uncertain; restart disabled")
                    return
                self._unavailable("worker_restarting", exc)
                try:
                    await self._retire()
                except Exception:
                    logger.warning("Preview process ownership uncertain; restart disabled")
                    return

    async def cad_descriptor(self) -> dict | None:
        """Off the loop and without importing OCP: the bundle this code may use, as the closed descriptor the
        command carries. Re-read on every STEP attempt — never a cached negative."""
        from backend.app import step_runtime

        found = await disk(step_runtime.locate, self.app_dir)
        return {"manifest_digest": found.digest, "bundle_key": found.key} if found.ok else None

    async def _run_or_revoke(self, command, ticket, timeout: float) -> dict:
        """The run RPC, raced against this ticket's revoke for a low attempt (spec §4.2). A revoke cancels only the
        reply wait — the service's work is ended by the cancel that follows, with its proof of end."""
        rpc = asyncio.ensure_future(self._rpc(command, timeout=timeout))
        if ticket.priority != "low":
            return await rpc
        waiter = asyncio.ensure_future(ticket.revoked.wait())
        try:
            done, _ = await asyncio.wait({rpc, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if rpc in done:
                return rpc.result()
            raise Revoked
        finally:
            waiter.cancel()
            if not rpc.done():
                rpc.cancel()
            await asyncio.gather(waiter, rpc, return_exceptions=True)

    @asynccontextmanager
    async def attempt(
        self,
        inputs: dict[str, tuple[Path | bytes, str]],
        *,
        deadline: int | None = None,
        priority: str | None = None,
        wait_seconds: float | None = None,
        order: int | None = None,
        on_admit: Callable[[], int | None] | None = None,
        on_hand_off: Callable[[], None] | None = None,
        publication=None,
        publication_optional: bool = False,
    ):
        """One owned preview attempt. ``priority`` defaults from the kind (sliced high, STL/OBJ normal, STEP low);
        a revoked low ends as the INTERNAL outcome ``preempted`` (no files, no publication) — its caller requeues it,
        no task was cancelled. ``order`` lets a requeued low keep its first admission order.

        ``on_admit`` runs once, synchronously, when the slot is granted; the absolute monotonic-ns deadline it
        returns bounds this attempt (a job's budgets run from its first admission). ``on_hand_off`` runs once,
        synchronously, at the hand-off of an ok result — the point of no return, under the arbiter's gate.

        ``publication`` (spec §5): a gate whose ``acquire()`` is asked in that same synchronous step, before the
        hand-off — a closed gate (restore) revokes the result, so nothing reaches its attach; the lease is held
        through the caller's publication and released when this context exits, and ``result.publishable`` says it
        was taken. Nothing waits for a pause inside the slot (Codex A-r3): a paused gate (a backup copying the
        pictures) DEFERS the attempt — at entry before anything is read, at the hand-off through the ordinary proof
        of end — and its owner retries after the pause. ``publication_optional`` (slicing, whose own output needs no
        publication right): a paused or closed gate hands the result off WITHOUT a lease — usable, not publishable."""
        kind = _inputs_kind(inputs)
        result = PreviewResult(kind=kind)
        priority = priority or PRIORITY_BY_KIND[kind]
        mesh = inputs.get("mesh")
        if (mesh and mesh[1] in CAD_KINDS and set(inputs) != {"mesh"}) or (
            "document" in inputs and set(inputs) != {"document"}
        ):
            # STEP never rides another attempt, and neither does a document (refused before any command)
            result.outcome = "protocol_error"
            yield result
            return
        if publication is not None and not publication_optional and publication.paused():
            # A backup is copying the pictures: no slot, no read, no conversion for a result that could not be
            # published (Codex A-r3 §6); the hand-off below catches a pause that begins later.
            result.outcome = "publication_deferred"
            logger.info("Preview attempt kind=%s outcome=publication_deferred stage=admission (backup pause)", kind)
            yield result
            return
        if wait_seconds is None:
            wait_seconds = HIGH_WAIT_SECONDS if priority == "high" else QUEUE_SECONDS
        wait_until = time.monotonic() + wait_seconds  # one deadline for everything before the slot (Codex A-R3)
        cad = None
        if kind == "step":
            cad = await self.cad_descriptor()
            if cad is None:
                # Nonfatal and local: no slot, no command, the service untouched (spec §6, B1).
                result.outcome = "cad_unavailable"
                logger.info("Preview attempt kind=step outcome=cad_unavailable stage=cad_import (no bundle)")
                yield result
                return
        wait_seconds = wait_until - time.monotonic()
        if wait_seconds <= 0:
            result.outcome = "busy"  # the bundle lookup spent the wait: no slot, no command
            yield result
            return
        if not self.ready or self.closed or self.waiters >= QUEUE_LIMIT:
            result.outcome = "busy" if self.ready else "unavailable"
            yield result
            return
        self.waiters += 1
        admitted_epoch = self.epoch
        ticket = None
        try:
            ticket = await self.slot.acquire(priority, wait_seconds, order=order)
        except TimeoutError:
            result.outcome = "busy"
        finally:
            self.waiters -= 1
        if ticket is None:
            yield result
            return
        attempt_id = uuid.uuid4().hex
        result.attempt_id = attempt_id
        owner = f" {PREVIEW_OWNER.get()}" if PREVIEW_OWNER.get() else ""
        root = self.staging / "main" / attempt_id
        deadline = min(
            deadline or time.monotonic_ns() + int(ACTIVE_SECONDS * 1e9), time.monotonic_ns() + int(ACTIVE_SECONDS * 1e9)
        )
        ticket.until = asyncio.get_running_loop().time() + max(0.0, (deadline - time.monotonic_ns()) / 1e9)
        command = None
        subscription = None
        lease = None
        revoked = False
        canceled = False
        checkpoint_tasks = set()
        transfer_nc = None
        began = time.monotonic()

        async def cleanup():
            if self.store and self.nc.is_connected:
                for role in ("mesh", "sliced", "source_png", "document", "preview", "checkpoint", "output"):
                    try:
                        await self.store.delete(f"{attempt_id}_{role}")
                    except Exception:
                        pass  # bucket TTL bounds interrupted/partial uploads
            if not self.uncertain:
                outcome = await disk(cleanup_owned, root)
                self._cleanup_result(outcome, "main_attempt", attempt_id)  # the failure is always explained
                if outcome.status == "retained_error":
                    moved = await self._quarantine(outcome, root, "main")
                    if moved.status != "quarantined":
                        self._dead_residue[root] = "main"

        try:
            try:
                if on_admit is not None:
                    owner_deadline = on_admit()
                    if owner_deadline is not None:
                        deadline = min(deadline, owner_deadline)
                if not self.ready or self.closed or self.epoch != admitted_epoch:
                    raise PreviewError("unavailable")
                self.active_task = asyncio.current_task()
                self.result_handed_off = False
                self.dependency_lost = False
                # A previous denied cleanup must not accumulate unbounded
                # staging across attempts. No other attempt owns this slot.
                await self._reclaim_dead_residue()
                residue = await disk(lambda: next((p for p in self.staging.rglob("*") if p.is_file()), None))
                if residue is not None:
                    issue = ("admission", str(residue))
                    log = logger.warning if issue != self._last_admission_issue else logger.debug
                    log("Preview staging_residue_blocks_admission: path=%s", residue)
                    self._last_admission_issue = issue
                    raise PreviewError("resource_limit")
                self._last_admission_issue = None
                await disk(reclaim_quarantine, self.quarantine)
                held, count = await disk(quarantine_usage, self.quarantine)
                if held + ATTEMPT_BYTES > STAGING_BYTES or count > QUARANTINE_ATTEMPTS:
                    logger.warning("Preview quarantine_blocks_admission: bytes=%d attempts=%d", held, count)
                    raise PreviewError("resource_limit")
                if ticket.revoke.is_set():
                    raise Revoked
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
                    for role, (source, input_kind) in inputs.items():
                        if role not in {"mesh", "sliced", "source_png", "document"} or input_kind not in KINDS:
                            raise PreviewError("protocol_error")
                        path = root / f"{role}.{input_kind}"
                        await disk(snapshot, source, path, deadline, revoke=ticket.revoke)
                        ref = await disk(describe, path, attempt_id, role, input_kind, deadline, revoke=ticket.revoke)
                        manifest.append(ref)
                        result.input_digests[role] = ref.digest
                        if sum(x.size for x in manifest) > ATTEMPT_BYTES:
                            raise PreviewError("resource_limit")
                        await put(transfer_store, path, ref, deadline, revoke=ticket.revoke)
                    command = self._command("run", manifest, attempt_id, deadline, cad)
                    logger.info("Preview attempt start attempt=%s kind=%s%s", attempt_id, kind, owner)

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
                            await get(transfer_store, ref, path, deadline, revoke=ticket.revoke)
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
                    reply = await self._run_or_revoke(command, ticket, remaining(deadline))
                    self._service_cleanup_reply(reply, attempt_id)
                    result.outcome = reply.get("outcome", "protocol_error")
                    if reply.get("stage") in STAGES:
                        result.stage = reply["stage"]
                    result.why, result.by, result.exit_code = reply_diagnostics(reply)
                    if not isinstance(result.outcome, str) or result.outcome not in OUTCOMES:
                        raise PreviewError("protocol_error")
                    if result.outcome in {"unavailable", "timeout", "protocol_error"}:
                        raise PreviewError(result.outcome)
                    if result.outcome == "ok":
                        await receive_files(reply.get("manifest", []))
                        # The point of no return, under the arbiter's gate: a revoke that came first wins, the
                        # ready output is discarded and the attempt ends preempted without any restart.
                        if publication is not None:
                            # None: closed (a restore) or paused (a backup copying the pictures). Never waited for
                            # here, holding the slot: a required publication is deferred or revoked, an optional
                            # one is handed off without the right to publish (Codex A-r3).
                            lease = publication.acquire()
                            if lease is None and not publication_optional:
                                raise PublicationDeferred if publication.paused() else Revoked
                        if not self.slot.hand_off(ticket):
                            if lease is not None:
                                lease.release()
                                lease = None
                            raise Revoked
                        result.publishable = lease is not None
                        if on_hand_off is not None:
                            on_hand_off()  # same synchronous step as the hand-off: nothing interleaves
                    else:
                        self.slot.hand_off(ticket)  # a service terminal: nothing left to interrupt or requeue
                    self.result_handed_off = True
            except Revoked as exc:
                revoked = True
                # internal: neither a public canceled nor a failure; a deferral is not even a preemption
                result.outcome = "publication_deferred" if isinstance(exc, PublicationDeferred) else "preempted"
                result.files.clear()
                if command:
                    try:
                        stopped_at = await owned(self._cancel(command))  # proof of end, or retire
                        result.stage = result.stage or stopped_at
                    except asyncio.CancelledError:
                        canceled = not self.dependency_lost
                    except Exception:
                        self.uncertain = True
            except asyncio.CancelledError:
                revoked = True
                canceled = not self.dependency_lost
                if canceled:
                    result.files.clear()
                result.outcome = "canceled" if canceled else "unavailable"
                # Only a run the service was told about needs its proof of end, or the retire; one cancelled while
                # its input was still copied or sent (a STEP job's Cancel lands there) leaves the healthy service be.
                if command:
                    try:
                        stopped_at = await owned(self._cancel(command))
                        result.stage = result.stage or stopped_at
                    except asyncio.CancelledError:
                        # Broker-loss callbacks may cancel this task again while
                        # the shielded retirement is being joined. The checkpoint
                        # remains usable; this is unavailable, not user cancel.
                        if not self.dependency_lost:
                            canceled = True
                            result.files.clear()
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
                        stopped_at = await owned(self._cancel(command))
                        result.stage = result.stage or stopped_at
                    except asyncio.CancelledError:
                        if self.dependency_lost:
                            result.outcome = "unavailable"
                        else:
                            canceled = True
                            result.files.clear()
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
                            await close_transfer(transfer_nc)
                        except Exception:
                            pass

                try:
                    await owned(close_transfers())
                except asyncio.CancelledError:
                    revoked = True
                    if self.dependency_lost:
                        result.outcome = "unavailable"
                    else:
                        canceled = True
                        result.files.clear()
                        result.outcome = "canceled"
                    try:
                        await owned(self._retire())
                    except BaseException:
                        self.uncertain = True
            if canceled:
                raise asyncio.CancelledError
            # every result is handed to the caller from here: a broker loss no longer cancels the caller's task
            self.result_handed_off = True
            yield result  # main validates/publishes while permit and staging still owned
        finally:

            async def finish():
                try:
                    await cleanup()
                finally:
                    self.active_task = None
                    self.result_handed_off = False
                    if not self.uncertain:
                        self.slot.release(ticket)
                    if lease is not None:
                        # Last: the publication AND this attempt's tail are over. Released earlier, a restore's
                        # drain proved quiet while the cleanup still ran and its runtime stop cancelled the tail.
                        lease.release()

            await owned(finish())
            said = (("why", result.why), ("by", result.by), ("exit", result.exit_code))
            logger.info(
                "Preview attempt=%s kind=%s outcome=%s stage=%s%s elapsed_ms=%d%s",
                attempt_id,
                kind,
                result.outcome,
                result.stage,
                "".join(f" {key}={value}" for key, value in said if value is not None),
                (time.monotonic() - began) * 1000,
                owner,
            )

    async def stop(self, *, from_owner: bool = False):
        """The ONE stop of this runtime, shared by every caller — a cancelled start, the module's shared stop, the
        owner's own give-up — and owned: a caller cancelled while it waits does not cut the stop short. The task
        that asks it (the owner giving up, an attempt's caller) is spared: the stop never waits for its caller,
        and such a caller still hears a cancellation the stop sends it."""
        caller = asyncio.current_task()
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop_once(spare=caller), name="preview-runtime-instance-stop")
        if from_owner or (caller is not None and caller in (self.starter, self.active_task)):
            await asyncio.shield(self._stop_task)
        else:
            await owned(self._stop_task)

    async def _stop_once(self, *, spare: asyncio.Task | None):
        self.closed = True
        self.ready = False
        if self.reason in {None, "starting", "worker_restarting", "not_started"}:
            self.reason = "stopped"
        starter = self.starter
        if starter is not None and not starter.done() and starter is not spare:
            # The start's owner first: its pause ends at once, an attempt's owned I/O and cleanup are waited for —
            # never cut by any deadline — and no attempt or monitor begins after this point (R5).
            starter.cancel()
            await asyncio.gather(starter, return_exceptions=True)
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        if self.active_task and self.active_task is not spare:
            self.active_task.cancel()
            await asyncio.gather(self.active_task, return_exceptions=True)
        try:
            await self._retire()
        except Exception:
            logger.warning("Preview process ownership uncertain; staging retained")
        await self._close_transport()
        if self.broker and not self.shared_broker:
            try:
                await disk(self.broker.stop)
            except Exception as exc:
                self._unavailable("recovery_required", exc)
        if not self.uncertain and hasattr(self, "staging") and not self.slot.locked():
            self._cleanup_result(await disk(cleanup_owned, self.staging), "generation")


runtime: PreviewRuntime | None = None


def get_preview_health() -> PreviewHealth:
    return runtime.health() if runtime else PreviewHealth(state="unavailable", reason="not_started")


async def start_preview_runtime(base: Path, app_dir: Path | None = None):
    global runtime
    from backend.app.services.local_worker_broker import start_local_worker_broker

    if _stopping is not None:
        raise RuntimeError("the previous preview runtime is still stopping")  # never a replacement beside it
    runtime = PreviewRuntime(base / ".cache" / "preview-service", app_dir=app_dir)
    try:
        broker = await start_local_worker_broker(base)
    except Exception as exc:
        from embedded_nats import RecoveryRequired

        runtime._unavailable("recovery_required" if isinstance(exc, RecoveryRequired) else "startup_failed", exc)
        if isinstance(exc, RecoveryRequired):
            logger.warning("Preview broker recovery refused: marker=%s; follow %s", exc.marker_path, RECOVERY_GUIDE)
        return
    runtime.shared_broker = broker
    await runtime.start()


# The one stop of `runtime`. Cleared, with the runtime, only by a PROVEN end; a stop that ended unproven, failed or
# was cancelled stays here done — its runtime with it — so every later restore, shutdown and start hears the same
# refusal until the process restarts (Codex A2-R1): «no runtime» is never mistaken for a proof.
_stopping: asyncio.Task | None = None


async def _stop_and_clear(stopping: PreviewRuntime) -> bool:
    global runtime, _stopping
    try:
        await stopping.stop()
    except Exception:
        logger.warning("Preview runtime stop failed; its ownership stays unproven until a restart", exc_info=True)
        return False
    if stopping.uncertain or stopping.slot.locked():
        logger.warning(
            "Preview runtime stop unproven: uncertain=%s slot_held=%s; refused until a restart",
            stopping.uncertain,
            stopping.slot.locked(),
        )
        return False
    if runtime is stopping:
        runtime = None  # only once the old runtime has really stopped: never a replacement beside it
    _stopping = None
    return True


def _stop_task() -> asyncio.Task | None:
    """The one stop of the current runtime: started once, shared by every caller (restore, shutdown, tests). A stop
    that ended without a proof is returned again, done, with its answer — never a second one."""
    global _stopping
    if _stopping is not None:
        return _stopping
    if runtime is None:
        return None
    _stopping = asyncio.ensure_future(_stop_and_clear(runtime))
    _stopping.set_name("preview-runtime-stop")
    return _stopping


async def stop_preview_runtime() -> bool:
    """Stop the runtime; True when its ownership ended proven (no uncertain service, no held slot)."""
    task = _stop_task()
    if task is None:
        return True
    try:
        return await asyncio.shield(task)  # a cancelled caller never cancels the stop it waits for
    except asyncio.CancelledError:
        if task.cancelled():
            return False  # the stop itself was cancelled (the loop is closing): no proof
        raise


async def stop_preview_runtime_within(seconds: float) -> bool:
    """The same stop, its answer bounded (Codex A-R2): an attempt inside an owned read of a hung share cannot be
    cancelled out of it, so past ``seconds`` the caller hears False while the stop goes on, owned and shared — the
    slot and the staging stay held, the singleton stays until that stop ends proven, and a later call waits for the
    same stop rather than starting another (or hears its negative answer again)."""
    task = _stop_task()
    if task is None:
        return True
    done, _ = await asyncio.wait({task}, timeout=seconds)
    if task not in done or task.cancelled() or task.exception() is not None:
        return False
    return task.result()


@asynccontextmanager
async def preview_attempt(inputs, **kwargs):
    if runtime is None:
        yield PreviewResult()
    else:
        async with runtime.attempt(inputs, **kwargs) as result:
            yield result
