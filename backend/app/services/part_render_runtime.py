"""Part-render runtime in main (spec §7, §13, §14; plan E3, task 18).

Owns the worker, its bucket and ONE slot. ``render`` holds the slot from the request to the end of its
cleanup. When the worker does not answer, the slot is held through the cancel and, if that is not
confirmed, through the retire and its proof. The proof covers the worker's tree and every attempt it ran
(``part_render_tree.tree_gone(strict=True)`` over the worker's staging). So no attempt ever starts beside
one that may still run -- not after a hung NAS read, a timeout or a cancellation. A retire that cannot
prove the tree gone leaves the runtime ``ownership_uncertain`` until the process restarts (spec §13); no
replacement worker starts and no proof is deleted.

The pattern is analysis/preview's (versioned commands, cancel, circuit breaker), not library's. It has an
owned spawn and a stop that never raises (plan E3, R6). Recovery covers every stage, from the broker
onwards, plus a Node that did not start (R16). The fetch directory belongs to ``render`` from its first
byte (R17).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from uuid import uuid4

from backend.app.services import part_render_tree, render_runtime
from backend.app.services.analysis_transport import AnalysisArtifact, get
from backend.app.services.local_worker_broker import get_local_worker_broker
from backend.app.services.part_render_node import NodeRenderError, node_env, verify_bundle
from backend.app.services.part_render_protocol import (
    ATTEMPT_BYTES,
    BUCKET_BYTES,
    BUCKET_TTL_SECONDS,
    PackError,
    unpack,
)
from backend.app.services.part_render_tree import generation_gone, tree_gone
from backend.app.services.part_render_types import AttemptResult, Mode, RenderTask, RuntimeUnavailable
from backend.app.services.preview_artifacts import disk, owned
from backend.app.services.preview_process import PreviewProcess, SpawnUnproven
from backend.app.services.preview_protocol import CONTROL_SECONDS, PreviewError, decode, encode
from backend.app.services.worker_staging import abandoned_attempts, cleanup_owned, retained_entries

logger = logging.getLogger(__name__)

_BUCKET_PREFIX = "bamdude_partrender_"  # not a prefix of any other worker's bucket, nor one of them a prefix of it
_HEX = re.compile(r"[0-9a-f]{32}\Z")
# after the deadline the worker still proves the tree gone (PreviewProcess.stop: up to ~17 s) and uploads the
# result (part_render_service._TRANSFER_SECONDS = 60); a shorter grace retired a healthy worker (final review M5)
_REPLY_GRACE_SECONDS = 90
_CANCEL_SECONDS = 30
_TRANSFER_SECONDS = 60
_STARTUP_SECONDS = 20
_RESTART_WINDOW_SECONDS = 60
_RESTART_LIMIT = 3
_CIRCUIT_SECONDS = 60
_NODE_PROBE_SECONDS = 10
_NODE_REPROBE_SECONDS = 60
_NODE_REFRESH_SECONDS = 30  # the installation and bundle looked up again by the monitor (R22)
_MAIN_STAGING_BYTES = 2 * ATTEMPT_BYTES  # retained fetch directories past this stop admission (R17)
_ATTEMPT_OUTCOMES = {"done", "timeout", "memory_limit", "crashed", "invalid_output", "canceled"}


class PartRenderRuntime:
    def __init__(self, base: Path, broker, *, app_dir: Path):
        self.base = base
        self.broker = broker
        self.app_dir = app_dir
        self.generation = uuid4().hex
        self.epoch = uuid4().hex
        self.bucket = f"{_BUCKET_PREFIX}{self.generation}"
        self.root = base / ".cache" / "part-render-service"
        self.staging = self.root / "staging" / self.generation
        self.slot = asyncio.Lock()
        self.sequence = 0
        self.restarts: list[float] = []
        self.circuit_until = 0.0
        self.service: PreviewProcess | None = None
        self.nc = None
        self.store = None
        self.monitor: asyncio.Task | None = None
        self.active_task: asyncio.Task | None = None
        self.interrupted_task: asyncio.Task | None = None
        self.ready = False
        self.closed = False
        self.uncertain = False
        # every earlier run proven over: until then restore is refused even without a scheduler (E3-I-R2)
        self.earlier_proven = False
        self.staging_over = False
        self.reason: str | None = "not_started"
        self.node: render_runtime.NodeInstall | None = None
        self.node_reason: str | None = "runtime_missing"
        self.node_started: tuple[str, str] | None = None  # (executable, version) seen to start
        self.node_failed: tuple[str, str] | None = None  # (executable, version) seen NOT to start
        self.node_reprobe_at = 0.0
        self.node_refresh_at = 0.0
        self.bundle_sha256: str | None = None
        self.queue: dict = {"pending": 0, "failed": 0}
        self.last: dict | None = None
        self.stats = {
            "attempts": 0,
            "done": 0,
            "failed": 0,
            "canceled": 0,
            "service_restart_attempts": 0,
            "staging_retained": 0,
        }

    # -- Node -----------------------------------------------------------------------------------------

    def resolve_node(self) -> None:
        """render_runtime.locate's three answers, never folded into one (consilium R5.3), the bundle, and whether
        THIS installation was seen to start: a new one is "starting" until the monitor probes it (E3-R8)."""
        try:
            found = render_runtime.locate(self.app_dir)
        except render_runtime.UnsupportedPlatform:
            self.node, self.node_reason, self.bundle_sha256 = None, "no_runtime", None
            return
        if found is None:
            self.node, self.node_reason = None, "runtime_missing"
            return
        try:
            self.bundle_sha256 = verify_bundle()
        except (NodeRenderError, OSError, ValueError):
            self.node, self.node_reason = None, "bundle_mismatch"
            return
        self.node = found
        identity = (str(found.executable), found.version)
        if identity == self.node_started:
            self.node_reason = None
        elif identity == self.node_failed:
            self.node_reason = "runtime_failed"
        else:
            self.node_reason = "starting"

    def _probe_node(self) -> None:
        """``node --version`` once for the installation in place; never on the event loop. A Node that is there
        but does not start is runtime_failed: a fault of the runtime, never of a file (spec §5.3)."""
        self.resolve_node()
        if self.node is None:
            return
        identity = (str(self.node.executable), self.node.version)
        try:
            done = subprocess.run(
                [str(self.node.executable), "--version"],
                env=node_env(),
                capture_output=True,
                timeout=_NODE_PROBE_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            started = done.returncode == 0 and done.stdout.decode("ascii", "replace").strip() == self.node.version
        except (OSError, subprocess.TimeoutExpired):
            started = False
        if started:
            self.node_started, self.node_failed = identity, None
        else:
            self.node_failed = identity
            logger.warning("Part render runtime_failed: Node %s does not start", self.node.version)
        self.resolve_node()

    def refresh_node_soon(self) -> None:
        """The child found the bundle changed: look at the installation again on the monitor's next tick."""
        self.node_refresh_at = 0.0

    async def _refresh_node_if_due(self) -> None:
        """The installation and the bundle, looked up again off the loop before the probe decision, so a Node
        put in place or replaced is seen without anybody reading health (consilium E3.2-R3). A new
        installation is "starting" (resolve_node) and probed on this very tick."""
        if time.monotonic() < self.node_refresh_at:
            return
        self.node_refresh_at = time.monotonic() + _NODE_REFRESH_SECONDS
        before = (str(self.node.executable), self.node.version) if self.node else None
        await disk(self.resolve_node)
        after = (str(self.node.executable), self.node.version) if self.node else None
        if after is not None and after != before:
            self.node_reprobe_at = 0.0  # not the replaced installation's reprobe interval

    async def _reprobe_node_if_due(self) -> None:
        """A new installation is probed at once; one that did not start, again after _NODE_REPROBE_SECONDS."""
        if self.node_reason not in ("starting", "runtime_failed") or time.monotonic() < self.node_reprobe_at:
            return
        was = self.node_reason
        await disk(self._probe_node)
        if self.node_reason == "runtime_failed":
            self.node_reprobe_at = time.monotonic() + _NODE_REPROBE_SECONDS
        elif was == "runtime_failed" and self.node_reason is None:
            logger.info("Part render runtime recovered: Node %s starts again", self.node.version)

    @property
    def unsupported(self) -> bool:
        return self.node_reason == "no_runtime"

    def refusal(self, mode: Mode) -> str | None:
        """Why an attempt of ``mode`` may not start now; None when it may."""
        if self.uncertain:
            return "ownership_uncertain"
        if self.closed:
            return "stopped"
        if not self.ready:
            return self.reason or "worker_restarting"
        if self.staging_over:
            return "staging_full"
        if mode == "render" and self.node_reason is not None:
            return self.node_reason
        return None

    def health(self) -> dict:
        """Reads only: the monitor keeps the installation current (R22); a System-page poll hashes nothing on
        the event loop and never races the monitor's own resolution (final review M4)."""
        if self.uncertain:
            state, reason = "unavailable", "ownership_uncertain"
        elif self.closed:
            state, reason = "unavailable", "stopped"
        elif not self.ready:
            state, reason = "unavailable", self.reason or "worker_restarting"
        elif self.staging_over:
            state, reason = "unavailable", "staging_full"
        elif self.node_reason == "no_runtime":
            state, reason = "degraded", "no_runtime"
        elif self.node_reason is not None:
            state, reason = "unavailable", self.node_reason
        else:
            state, reason = "ready", None
        try:
            pinned = render_runtime.load_manifest().get("pinned_at")
        except (OSError, ValueError):
            pinned = None
        return {
            "state": state,
            "reason": reason,
            "runtime": {"version": self.node.version, "pinned_at": pinned} if self.node else None,
            "bundle": {"sha256": self.bundle_sha256} if self.bundle_sha256 else None,
            "queue": dict(self.queue),
            "last": self.last,
            "stats": dict(self.stats),
        }

    # -- worker ---------------------------------------------------------------------------------------

    def _directories(self) -> None:
        for path in (self.root, self.staging / "main", self.staging / "service"):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _prove_previous_generations(self) -> None:
        """An attempt of an earlier process that cannot be proven over closes admission: its reader may still
        hang on the share this process would read next. The proof is the records of each earlier generation,
        parents first (``part_render_tree.generation_gone``); once every earlier run is proven over, its
        staging has no owner and goes (final review C2, consilium E3-I-R1)."""
        staging = self.staging.parent
        earlier = (
            [
                path
                for path in staging.iterdir()
                if path != self.staging and _HEX.fullmatch(path.name) and path.is_dir() and not path.is_symlink()
            ]
            if staging.is_dir()
            else []
        )
        for generation in earlier:
            if not generation_gone(generation):
                self.uncertain, self.reason = True, "ownership_uncertain"
                logger.error("Part render ownership uncertain: run %s is not proven over", generation.name[:8])
                return
        for generation in earlier:
            cleanup = cleanup_owned(generation)
            if cleanup.status == "retained_error":
                logger.warning("Part render earlier run staging retained: %s", cleanup.error_type)
        if earlier:
            logger.info("Part render earlier runs over: staging of %d removed", len(earlier))
        self.earlier_proven = True

    def _report_retained_staging(self) -> None:
        empty = 0
        for path, classification, reason in retained_entries(self.staging.parent, self.staging, "partrender"):
            if classification == "empty_skeleton":
                empty += 1
                continue
            logger.warning(
                "Retained part render staging: path=%s classification=%s reason=%s; verify owners before manual cleanup",
                path.name,
                classification,
                reason,
            )
        if empty:
            logger.info("Part render staging empty_skeleton_count=%d; no manual cleanup required", empty)

    def _main_staging_bytes(self) -> int:
        main = self.staging / "main"
        return sum(path.stat().st_size for path in main.rglob("*") if path.is_file()) if main.is_dir() else 0

    def command(
        self,
        operation: str,
        attempt_id: str | None = None,
        task: dict | None = None,
        node: str | None = None,
        deadline_ns: int = 0,
    ) -> dict:
        self.sequence += 1
        return {
            "version": 1,
            "generation": self.generation,
            "epoch": self.epoch,
            "attempt_id": attempt_id or uuid4().hex,
            "sequence": self.sequence,
            "operation": operation,
            "deadline_ns": deadline_ns or time.monotonic_ns() + CONTROL_SECONDS * 10**9,
            "task": task,
            "node": node,
        }

    async def rpc(self, command: dict, timeout: float) -> dict:
        message = await self.nc.request(
            f"bamdude.partrender.{self.generation}.{self.epoch}", encode(command), timeout=timeout
        )
        return decode(message.data)

    async def start(self) -> None:
        """Fail-soft. Whatever does not come up now -- the broker, the connection, the bucket, the worker -- the
        monitor brings up later, with a bounded backoff and a circuit (consilium E3-R9). Only
        ownership_uncertain stops that for good."""
        self.reason = "starting"
        await disk(self._directories)
        # this run records itself before it spawns anything, so a later start can prove it over (E3-I-R1)
        await disk(part_render_tree.record, self.staging / "owner.pid", os.getpid())
        try:
            await disk(self._prove_previous_generations)
        except Exception:
            # no proof is no proof: records kept, admission and restore closed (consilium E3-I-R2)
            self.uncertain, self.reason = True, "ownership_uncertain"
            logger.exception("Part render ownership uncertain: the proof of earlier runs failed")
        self._report_retained_staging()
        await disk(self._probe_node)
        if self.uncertain:
            return
        try:
            await self._initialize()
        except Exception as exc:
            if not self.uncertain:
                self.reason = "startup_failed"
            logger.warning("Part render worker unavailable at startup: %s", type(exc).__name__)
        self.monitor = asyncio.create_task(self._monitor(), name="part-render-service-monitor")

    async def _create_bucket(self, js) -> None:
        from nats.js.api import ObjectStoreConfig

        # each bucket reserves max_bytes against the broker's max_file_store: an earlier generation's goes first
        for stream in await js.streams_info():
            if stream.config.name.startswith(f"OBJ_{_BUCKET_PREFIX}"):
                await js.delete_stream(stream.config.name)
        self.store = await js.create_object_store(
            bucket=self.bucket,
            config=ObjectStoreConfig(
                bucket=self.bucket, max_bytes=BUCKET_BYTES, ttl=BUCKET_TTL_SECONDS, storage="file"
            ),
        )

    async def _initialize(self) -> None:
        """Every stage that is not up yet, in order; a stage that is up is left alone (no second worker)."""
        import nats

        if self.broker is None:
            self.broker = get_local_worker_broker()
            if self.broker is None:
                raise RuntimeError("local worker broker unavailable")
        if self.nc is None or not self.nc.is_connected:
            self.nc = await nats.connect(
                self.broker.url,
                token=self.broker.token,
                allow_reconnect=False,
                connect_timeout=CONTROL_SECONDS,
                closed_cb=self.disconnected,
            )
            self.store = None
        if self.store is None:
            await self._create_bucket(self.nc.jetstream(timeout=CONTROL_SECONDS))
        if self.service is None or self.service.process.poll() is not None:
            if self.service is not None:
                await self.retire()
            await self.launch()
            logger.info(
                "Part render worker ready pid=%s node=%s",
                self.service.process.pid,
                self.node.version if self.node else self.node_reason,
            )

    async def launch(self) -> None:
        self.epoch = uuid4().hex

        async def spawn():
            await disk(part_render_tree.launch, self.staging, "worker")
            try:
                self.service = await disk(
                    PreviewProcess,
                    "backend.app.part_render_service",
                    {
                        "url": self.broker.url,
                        "token": self.broker.token,
                        "bucket": self.bucket,
                        "generation": self.generation,
                        "epoch": self.epoch,
                        "staging": str(self.staging / "service"),
                    },
                    self.root / "cache",
                    on_spawn=lambda pid: part_render_tree.record(self.staging / "worker.pid", pid),
                )
            except SpawnUnproven as exc:
                # its guardian existed, its start failed, and nothing proved it gone: no worker beside it
                # until the process restarts (consilium E3.2-R1). Set inside the owned task.
                self.uncertain, self.reason = True, "ownership_uncertain"
                logger.error("Part render worker ownership uncertain: guardian pid=%s is not proven gone", exc.pid)
                raise

        await owned(spawn())
        deadline = time.monotonic() + _STARTUP_SECONDS
        while time.monotonic() < deadline:
            if self.service.process.poll() is not None:
                raise RuntimeError("part render worker exited")
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
        raise RuntimeError("part render worker startup timeout")

    async def disconnected(self) -> None:
        self.ready, self.reason = False, "broker_disconnected"
        logger.warning("Part render worker lost its broker")
        if self.active_task is not None:
            self.interrupted_task = self.active_task
            self.active_task.cancel()

    async def retire(self) -> None:
        """Stop the worker and prove every attempt it ran gone; ``ownership_uncertain`` when it cannot, with
        every proof left where it is."""
        self.ready = False
        service = self.service
        if service is not None:
            try:
                await disk(service.stop)
            except Exception as exc:
                self.uncertain, self.reason = True, "ownership_uncertain"
                logger.error("Part render worker ownership uncertain: its process tree is not proven gone")
                raise RuntimeUnavailable("ownership_uncertain") from exc
            self.service = None
        service_root = self.staging / "service"
        if not await disk(service_root.is_dir):
            return
        attempts, unknown = await disk(abandoned_attempts, service_root)
        for path in unknown:
            logger.warning("Part render worker staging holds an unknown entry: %s", path.name)
        # all attempts first, deletions after: one unproven attempt keeps every directory (consilium E3-R1)
        for attempt in attempts:
            # strict: the worker is gone, so a launch without its record may have left a process outside
            # every tree this process can see
            if not await disk(lambda path=attempt: tree_gone(path, strict=True)):
                self.uncertain, self.reason = True, "ownership_uncertain"
                logger.error(
                    "Part render attempt=%s ownership uncertain: a launched process is not proven gone",
                    attempt.name[:8],
                )
                raise RuntimeUnavailable("ownership_uncertain")
        for attempt in attempts:
            result = await disk(cleanup_owned, attempt)
            if result.status == "retained_error":
                logger.warning("Part render attempt=%s staging retained: %s", attempt.name[:8], result.error_type)

    def _healthy(self) -> bool:
        """A worker that is alive is not yet a worker that works: without its confirmed ready it is retired and
        replaced like a dead one (consilium E3.2-R4)."""
        return (
            self.ready
            and self.service is not None
            and self.service.process.poll() is None
            and self.nc is not None
            and self.nc.is_connected
            and self.store is not None
        )

    async def _monitor(self) -> None:
        while not self.closed:
            await asyncio.sleep(0.5)
            try:
                await self._refresh_node_if_due()
                await self._reprobe_node_if_due()
            except Exception:
                logger.warning("Part render Node probe failed", exc_info=True)
            if self._healthy():
                continue
            self.ready = False
            if self.active_task is not None:
                if self.interrupted_task is not self.active_task:
                    self.interrupted_task = self.active_task
                    self.active_task.cancel()  # render's own path retires and proves the tree, under the slot
                continue
            if self.slot.locked() or self.uncertain:
                continue
            if self.service is not None:
                try:
                    await self.retire()
                except Exception:
                    return  # ownership_uncertain: no replacement until the process restarts (spec §13)
            now = time.monotonic()
            self.restarts = [moment for moment in self.restarts if now - moment < _RESTART_WINDOW_SECONDS]
            if now < self.circuit_until:
                self.reason = "circuit_open"
                continue
            if len(self.restarts) >= _RESTART_LIMIT:
                self.circuit_until = now + _CIRCUIT_SECONDS
                self.reason = "circuit_open"
                logger.warning("Part render worker recovery circuit open for %ds", _CIRCUIT_SECONDS)
                continue
            self.reason = "worker_restarting"
            await asyncio.sleep(2 ** len(self.restarts))
            self.restarts.append(time.monotonic())
            self.stats["service_restart_attempts"] += 1
            try:
                await self._initialize()
            except Exception as exc:
                logger.warning("Part render worker recovery failed: %s", type(exc).__name__)
                if self.uncertain:
                    return  # a start not proven gone (E3.2-R1): no replacement until the process restarts
                if self.service is not None:
                    try:
                        await self.retire()
                    except Exception:
                        return

    # -- one attempt ----------------------------------------------------------------------------------

    async def _cancel_or_retire(self, command: dict) -> None:
        """The worker did not answer the run: the slot stays held until the attempt is proven over. Only a
        "canceled" is proof; "unavailable" from a cached unproven end means retire (consilium E3-R2)."""
        if self.nc is not None and self.nc.is_connected:
            try:
                reply = await self.rpc({**command, "operation": "cancel"}, _CANCEL_SECONDS)
                if reply.get("outcome") == "canceled":
                    return
            except Exception:
                pass
        await self.retire()  # RuntimeUnavailable("ownership_uncertain") when the proof fails

    async def _fetch(self, artifact: dict, attempt: str, root: Path) -> Path:
        ref = AnalysisArtifact.parse(artifact, attempt, "partrender", ATTEMPT_BYTES)
        await disk(root.mkdir)
        packed = root / "result.bin"
        await get(self.store, ref, packed, time.monotonic_ns() + _TRANSFER_SECONDS * 10**9)
        files = root / "files"
        await disk(files.mkdir)
        await disk(unpack, packed, files)
        await disk(packed.unlink)
        return files

    async def _drop(self, root: Path, attempt: str) -> None:
        """The fetch directory of an attempt that produced no files: removed, or counted as retained."""
        if not await disk(root.exists):
            return
        cleanup = await disk(cleanup_owned, root)
        if cleanup.status == "retained_error":
            self.stats["staging_retained"] += 1
            logger.warning("Part render attempt=%s main staging retained: %s", attempt[:8], cleanup.error_type)

    async def _forget(self, attempt: str) -> None:
        try:
            async with asyncio.timeout(CONTROL_SECONDS):
                await self.store.delete(f"{attempt}_partrender")
        except Exception:
            pass  # absent, or the bucket's TTL takes it

    async def render(self, task: RenderTask, *, mode: Mode, deadline_ns: int) -> AttemptResult:
        async with self.slot:
            self.staging_over = await disk(self._main_staging_bytes) > _MAIN_STAGING_BYTES
            if self.staging_over:
                logger.warning("Part render main staging is over its budget; admission waits for its cleanup")
            refused = self.refusal(mode)
            if refused is not None:
                raise RuntimeUnavailable(refused)
            install = self.node  # read once: the monitor's thread may change it under this attempt (M4)
            if mode == "render" and install is None:
                raise RuntimeUnavailable(self.node_reason or "runtime_missing")
            attempt = uuid4().hex
            main_root = self.staging / "main" / attempt  # owned by this call from before its first byte (R17)
            node = str(install.executable) if mode == "render" else None
            command = self.command("run", attempt, task.wire(), node, deadline_ns)
            result = AttemptResult("crashed", attempt, 0, runtime_version=install.version if node else None)
            started = time.monotonic()
            settled = False
            logger.info(
                "Part render attempt=%s render_id=%s file=%s plate=%s mode=%s started",
                attempt[:8],
                task.render_id,
                task.file_sha256[:12],
                task.plate_index,
                mode,
            )
            self.active_task = asyncio.current_task()
            self.stats["attempts"] += 1
            try:
                try:
                    encode(command)
                except PreviewError:
                    # past the worker's wire limit (a path of thousands of characters): the file's problem, a
                    # failed attempt -- never a retire of the healthy worker (final review M7)
                    logger.warning("Part render attempt=%s task does not fit the worker's wire", attempt[:8])
                    return result
                timeout = max(0.0, (deadline_ns - time.monotonic_ns()) / 1e9) + _REPLY_GRACE_SECONDS
                reply = await self.rpc(command, timeout)
                settled = True  # the worker answered: the attempt's tree is proven gone, or it says it is not
                outcome = reply.get("outcome")
                if outcome == "unavailable":
                    await owned(self.retire())
                    raise RuntimeUnavailable(self.reason or "worker_restarting")
                if outcome not in _ATTEMPT_OUTCOMES:
                    raise RuntimeError(f"part render worker answered {outcome!r}")
                result.outcome = outcome
                if outcome == "done":
                    result.result = reply.get("result")
                    if reply.get("artifact") is not None:
                        try:
                            result.files = await self._fetch(reply["artifact"], attempt, main_root)
                        except (PreviewError, PackError, OSError):
                            result.outcome, result.result = "invalid_output", None
                return result
            except asyncio.CancelledError:
                result.outcome = "canceled"
                if not settled:
                    await owned(self._cancel_or_retire(command))
                if self.interrupted_task is asyncio.current_task():
                    # the monitor's cancel, not the caller's: the caller gets a refusal and its task stays usable
                    asyncio.current_task().uncancel()
                    raise RuntimeUnavailable(self.reason or "worker_restarting") from None
                raise
            except RuntimeUnavailable:
                result.outcome = "unavailable"
                raise
            except Exception as exc:
                if not settled:
                    await owned(self._cancel_or_retire(command))  # RuntimeUnavailable when unproven
                result.outcome = "timeout" if time.monotonic_ns() >= deadline_ns else "crashed"
                logger.warning("Part render attempt=%s worker did not answer: %s", attempt[:8], type(exc).__name__)
                return result
            finally:
                if self.interrupted_task is asyncio.current_task():
                    self.interrupted_task = None
                self.active_task = None
                await owned(self._forget(attempt))
                if result.files is None:
                    await owned(self._drop(main_root, attempt))  # after its own I/O, failure or cancel alike
                result.elapsed_ms = int((time.monotonic() - started) * 1000)
                self._log_end(result)

    def _log_end(self, result: AttemptResult) -> None:
        child = result.result or {}
        counts = child.get("methods") or {}
        self.last = {"outcome": result.outcome, "elapsed_ms": result.elapsed_ms}
        key = {"done": "done", "canceled": "canceled"}.get(result.outcome, "failed")
        self.stats[key] += 1
        logger.log(
            logging.INFO if result.outcome == "done" and child.get("outcome") == "ok" else logging.WARNING,
            "Part render attempt=%s outcome=%s result=%s reason=%s elapsed_ms=%d methods=%s",
            result.attempt_id[:8],
            result.outcome,
            child.get("outcome", "-"),
            child.get("reason") or "-",
            result.elapsed_ms,
            ",".join(f"{name}:{count}" for name, count in sorted(counts.items()) if count) or "-",
        )

    async def discard(self, result: AttemptResult) -> None:
        if result.files is not None:
            await owned(self._drop(result.files.parent, result.attempt_id))

    async def stop(self) -> None:
        """Never raises: lifespan shutdown must still reach the broker's stop (plan E3, R6)."""
        self.closed = True
        if self.monitor is not None:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        if self.active_task is not None and self.active_task is not asyncio.current_task():
            self.active_task.cancel()
            await asyncio.gather(self.active_task, return_exceptions=True)
        try:
            async with self.slot:
                await self.retire()
        except Exception:
            logger.warning("Part render worker ownership uncertain at stop; staging retained")
        if not self.uncertain:
            cleanup = await disk(cleanup_owned, self.staging)
            if cleanup.status == "retained_error":
                logger.warning("Part render generation staging retained: %s", cleanup.error_type)
        if self.nc is not None:
            try:
                await self.nc.close()
            except Exception:
                pass


runtime: PartRenderRuntime | None = None


def get_part_render_health() -> dict:
    if runtime is None:
        return {
            "state": "unavailable",
            "reason": "not_started",
            "runtime": None,
            "bundle": None,
            "queue": None,
            "last": None,
            "stats": None,
        }
    return runtime.health()


def get_part_render_runtime() -> PartRenderRuntime | None:
    return runtime


async def start_part_render_runtime(base: Path, app_dir: Path) -> PartRenderRuntime:
    global runtime
    runtime = PartRenderRuntime(base, get_local_worker_broker(), app_dir=app_dir)
    await runtime.start()
    return runtime


async def stop_part_render_runtime() -> None:
    global runtime
    current, runtime = runtime, None
    if current is not None:
        await current.stop()
