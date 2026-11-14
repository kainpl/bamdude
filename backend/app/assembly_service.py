"""Independent local NATS service of "Assemble project": a queue and one disposable packing child per job.

Main stages a directory on the shared file system and names it in ``run``; the service answers at once and
main asks ``status`` until the job is done. Nothing but small JSON crosses the wire: the child's result stays
in ``<root>/result.json`` for main to read.

    ready                      the service answers and its epoch matches
    run {root, budget_ns}      queue the job (``busy`` beyond the queue's bound)
    status                     queued (with its position) / running (with the child's progress) / done / unknown
    cancel                     answered only after the job's child is proven gone
    shutdown

One job runs at a time; the queue lives here, so main never waits on a lock. A job gets a NEW child: it
parses untrusted meshes and may hold gigabytes, and nothing of it may reach the next job.

A STEP source is converted HERE first: main stages it as ``<root>/step/<part>/input.step`` and the job names
``step/<part>/mesh.stl``. Each file gets its own disposable converter (``services/step_thumbnail`` in print
mode — the only importer of OCP), proven gone before the next starts; the packing child sees meshes alone.
A conversion that fails ends the job with the part named in ``result.json`` — never a layout without it.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from collections import deque
from pathlib import Path

import nats

from backend.app.services.preview_artifacts import disk, owned
from backend.app.services.preview_process import PreviewProcess, SpawnUnproven
from backend.app.services.preview_protocol import (
    CAD_PRINT_TRIANGLES,
    CONTROL_SECONDS,
    JSON_BYTES,
    PreviewError,
    closed_why,
    decode,
    encode,
)

QUEUE_LIMIT = 16  # jobs waiting behind the running one
MEMORY_BYTES = 2 * 1024**3
STAGING_BYTES = 2 * 1024**3
RESULT_BYTES = 256 * 1024
PROGRESS_BYTES = 4096
MAX_BUDGET_NS = 3600 * 10**9
_GRACE_NS = 5 * 10**9  # the child stops itself at the deadline; this is for one hung in native code
_WATCH_SECONDS = 0.5
_DONE_LIMIT = 256
_HEX = re.compile(r"[0-9a-f]{32}\Z")
_KEYS = {"version", "generation", "epoch", "attempt_id", "operation", "root", "budget_ns"}
_PROGRESS_KEYS = ("printer_index", "printer_count", "run", "runs", "placed", "total", "fill_part")
PACK_MODULE = "backend.app.assembly_child"
STEP_MODULE = "backend.app.services.step_thumbnail"
STEP_PARTS = 100  # the request's own bound on files
_STEP_DIR = re.compile(r"(0|[1-9]\d{0,2})\Z")
#: what a converter's refusal is called in the job's result; anything else is ``step_failed``
_STEP_REASONS = {"resource_limit": "step_too_large", "cad_unavailable": "step_unavailable", "timeout": "timeout"}


class _Stop(Exception):
    def __init__(self, outcome: str, why: str):
        super().__init__(why)
        self.outcome, self.why = outcome, why


class Job:
    def __init__(self, attempt: str, root: Path, budget_ns: int):
        self.attempt, self.root, self.budget_ns = attempt, root, budget_ns
        self.state = "queued"
        self.progress: dict | None = None
        self.result: dict | None = None
        self.task: asyncio.Task | None = None


def _tree_bytes(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _read_progress(root: Path) -> dict | None:
    """The child's progress, reduced to the whole numbers the wire carries; anything else is no progress."""
    try:
        raw = (root / "progress.json").read_bytes()
        if len(raw) > PROGRESS_BYTES:
            return None
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    progress = {}
    for key in _PROGRESS_KEYS:
        value = data.get(key)
        if value is None:
            continue
        if type(value) is not int or not 0 <= value < 2**31:
            return None
        progress[key] = value
    return progress


def _read_result(root: Path) -> tuple[str, str]:
    """(outcome, why) the child wrote. The file is the child's: read with a cap and trusted for two words only."""
    try:
        path = root / "result.json"
        if path.stat().st_size > RESULT_BYTES:
            return "failed", "result_too_large"
        data = json.loads(path.read_bytes())
    except FileNotFoundError:
        return "failed", "no_result"
    except (OSError, ValueError, RecursionError):
        return "failed", "bad_result"
    if not isinstance(data, dict) or data.get("outcome") not in (
        "ok",
        "failed",
        "timeout",
        "canceled",
        "resource_limit",
    ):
        return "failed", "bad_result"
    return data["outcome"], closed_why(data.get("why")) or ""


def _step_parts(root: Path) -> list[int]:
    """The parts main staged as STEP: ``step/<part>/input.step``, in part order."""
    parts = []
    try:
        entries = list((root / "step").iterdir())
    except OSError:
        return []
    for entry in entries:
        if _STEP_DIR.fullmatch(entry.name) and not entry.is_symlink() and (entry / "input.step").is_file():
            parts.append(int(entry.name))
    return sorted(parts)


def _cad_site(app_dir: Path) -> str | None:
    """The installed CAD bundle of this application, located by the service itself; None when there is none."""
    from backend.app import step_runtime  # stdlib-only: it never imports OCP

    try:
        found = step_runtime.locate(app_dir)
    except Exception:
        return None
    return str(found.site) if found.ok and found.site is not None else None


def _read_conversion(directory: Path) -> str:
    """ "" when the converter left the mesh its result names; else the reason. The files are the child's: read
    with a cap, and believed only as far as the mesh's own size agrees. Its input and notes are removed."""
    reason = "step_failed"
    try:
        path = directory / "result.json"
        data = json.loads(path.read_bytes()) if path.stat().st_size <= JSON_BYTES else None
    except (OSError, ValueError, RecursionError):
        data = None
    if isinstance(data, dict):
        outcome, triangles = data.get("outcome"), data.get("triangles")
        if outcome == "ok":
            try:
                mesh = directory / "mesh.stl"
                if (
                    type(triangles) is int
                    and 0 < triangles <= CAD_PRINT_TRIANGLES
                    and mesh.is_file()
                    and not mesh.is_symlink()
                    and mesh.stat().st_size == 84 + 50 * triangles
                ):
                    reason = ""
            except OSError:
                pass
        elif isinstance(outcome, str):
            reason = _STEP_REASONS.get(outcome, reason)
    for name in ("input.step", "result.json", "result.json.part", "stage", "stage.part", "mesh.stl.part"):
        (directory / name).unlink(missing_ok=True)
    return reason


def _write_step_failure(root: Path, part: int, reason: str) -> None:
    payload = {"outcome": "failed", "why": reason, "parts": [{"index": part, "reason": reason}]}
    (root / "result.json").write_text(json.dumps(payload), encoding="utf-8")


async def _proven_stop(child) -> bool:
    """Stop the child and say whether its tree is proven gone. A cancelled caller still waits for the answer."""
    stopper = asyncio.ensure_future(asyncio.to_thread(child.stop))
    while not stopper.done():
        try:
            await asyncio.shield(stopper)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    return stopper.exception() is None


class Service:
    def __init__(self, config: dict):
        self.config = config
        self.subject = f"bamdude.assembly.{config['generation']}.{config['epoch']}"
        self.staging = Path(config["staging"])
        self.cache = Path(config["cache"])
        self.app_dir = Path(config.get("app_dir") or Path(__file__).resolve().parents[2])
        self.queue: deque[Job] = deque()
        self.jobs: dict[str, Job] = {}
        self.done: dict[str, Job] = {}
        self.running: Job | None = None
        self.wake = asyncio.Event()
        self.stopping = asyncio.Event()
        self.handlers: set[asyncio.Task] = set()
        self.uncertain = False
        self.worker: asyncio.Task | None = None

    async def start(self):
        self.nc = await nats.connect(
            self.config["url"],
            token=self.config["token"],
            allow_reconnect=False,
            connect_timeout=CONTROL_SECONDS,
            closed_cb=self.disconnected,
        )
        await self.nc.subscribe(self.subject, cb=self.receive)
        await self.nc.flush(timeout=CONTROL_SECONDS)
        self.worker = asyncio.create_task(self.work(), name="assembly-queue")

    async def disconnected(self):
        self.stopping.set()

    async def receive(self, message):
        if len(self.handlers) >= 64:
            await message.respond(encode({"outcome": "busy"}))
            return
        task = asyncio.create_task(self.handle(message))
        self.handlers.add(task)
        task.add_done_callback(self.handlers.discard)

    def parse(self, raw: bytes) -> dict:
        data = decode(raw)
        if set(data) != _KEYS:
            raise PreviewError("protocol_error")
        if (
            type(data["version"]) is not int
            or data["version"] != 1
            or data["generation"] != self.config["generation"]
            or data["epoch"] != self.config["epoch"]
            or not isinstance(data["attempt_id"], str)
            or not _HEX.fullmatch(data["attempt_id"])
            or data["operation"] not in ("ready", "run", "status", "cancel", "shutdown")
        ):
            raise PreviewError("protocol_error")
        if data["operation"] == "run":
            budget = data["budget_ns"]
            if type(budget) is not int or not 0 < budget <= MAX_BUDGET_NS or not isinstance(data["root"], str):
                raise PreviewError("protocol_error")
            root = Path(data["root"])
            try:
                resolved = root.resolve(strict=True)
                resolved.relative_to(self.staging.resolve(strict=True))
            except (OSError, ValueError) as exc:
                raise PreviewError("protocol_error") from exc
            if not resolved.is_dir() or resolved == self.staging.resolve():
                raise PreviewError("protocol_error")
            data["root"] = resolved
        elif data["root"] is not None or data["budget_ns"] is not None:
            raise PreviewError("protocol_error")
        return data

    async def handle(self, message):
        try:
            result = await self.command(self.parse(message.data))
        except (PreviewError, ValueError) as exc:
            result = {"outcome": exc.outcome if isinstance(exc, PreviewError) else "protocol_error"}
        except Exception:
            result = {"outcome": "unavailable"}
        try:
            await message.respond(encode(result))
        except Exception:
            pass

    def describe(self, job: Job) -> dict:
        reply = {"outcome": "ok", "state": job.state}
        if job.state == "queued":
            reply["position"] = self.queue.index(job) + (1 if self.running else 0)
        elif job.state == "running":
            reply["progress"] = job.progress
        else:
            reply["result"] = job.result
        return reply

    async def command(self, command: dict) -> dict:
        operation, attempt = command["operation"], command["attempt_id"]
        if operation == "ready":
            return {
                "outcome": "unavailable" if self.uncertain else "ok",
                "epoch": self.config["epoch"],
                "version": 1,
                "monotonic_ns": time.monotonic_ns(),
            }
        if operation == "shutdown":
            self.stopping.set()
            return {"outcome": "ok"}
        job = self.jobs.get(attempt) or self.done.get(attempt)
        if operation == "status":
            return self.describe(job) if job else {"outcome": "ok", "state": "unknown"}
        if operation == "run":
            if job:
                return self.describe(job)  # the same request again
            if self.uncertain:
                return {"outcome": "unavailable"}
            if len(self.queue) >= QUEUE_LIMIT:
                return {"outcome": "busy"}
            job = self.jobs[attempt] = Job(attempt, command["root"], command["budget_ns"])
            self.queue.append(job)
            self.wake.set()
            return self.describe(job)
        # cancel
        if job is None or job.state == "done":
            return {"outcome": "canceled"}
        if job.state == "queued":
            self.queue.remove(job)
            self.finish(job, "canceled", "cancel", 0.0)
            return {"outcome": "canceled"}
        job.task.cancel()
        await owned(asyncio.gather(job.task, return_exceptions=True))
        return {"outcome": "unavailable" if self.uncertain else "canceled"}

    def finish(self, job: Job, outcome: str, why: str, elapsed: float):
        job.state = "done"
        job.result = {"outcome": outcome, "why": why, "elapsed_ms": int(elapsed * 1000)}
        self.jobs.pop(job.attempt, None)
        self.done[job.attempt] = job
        if len(self.done) > _DONE_LIMIT:
            self.done.pop(next(iter(self.done)))

    async def work(self):
        while not self.stopping.is_set():
            if not self.queue or self.uncertain:
                self.wake.clear()
                await self.wake.wait()
                continue
            job = self.running = self.queue.popleft()
            job.state = "running"
            job.task = asyncio.create_task(self.execute(job), name=f"assembly-{job.attempt}")
            await asyncio.gather(job.task, return_exceptions=True)
            self.running = None

    async def _child(self, job: Job, module: str, payload: dict, deadline: int, *, progress: bool) -> None:
        """One disposable child to its end, watched; its tree is proven gone before this returns."""
        child = None
        try:

            async def spawn():
                nonlocal child
                child = await disk(PreviewProcess, module, payload, self.cache, job_memory_limit=MEMORY_BYTES)

            await owned(spawn())
            watched = 0.0
            while child.process.poll() is None:
                if time.monotonic_ns() >= deadline + _GRACE_NS:
                    raise _Stop("timeout", "deadline")
                if time.monotonic() - watched >= _WATCH_SECONDS:
                    watched = time.monotonic()
                    if await disk(child.rss) > MEMORY_BYTES:
                        raise _Stop("resource_limit", "memory")
                    if await disk(_tree_bytes, job.root) > STAGING_BYTES:
                        raise _Stop("resource_limit", "staging")
                    if progress:
                        job.progress = await disk(_read_progress, job.root) or job.progress
                await asyncio.sleep(0.05)
        finally:
            if child is not None and not await _proven_stop(child):
                # no proof that the child is gone: nothing else starts here
                self.uncertain = True
                raise _Stop("unavailable", "ownership_uncertain")
            # the reaping waits through a cancellation on purpose; a stop that landed meanwhile still ends the job
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise asyncio.CancelledError

    async def _convert(self, job: Job, deadline: int) -> None:
        """Every STEP source to its mesh, in part order. Raises ``_Stop`` with the part named in result.json."""
        parts = await disk(_step_parts, job.root)
        if not parts:
            return
        if len(parts) > STEP_PARTS:
            raise _Stop("failed", "bad_job")
        site = await disk(_cad_site, self.app_dir)
        for number, part in enumerate(parts, start=1):
            job.progress = {"convert": number, "convert_count": len(parts)}
            reason = "step_unavailable"
            if site is not None:
                directory = job.root / "step" / str(part)
                payload = {"root": str(directory), "deadline": deadline, "site": site, "mode": "print"}
                try:
                    await self._child(job, STEP_MODULE, payload, deadline, progress=False)
                except _Stop as stop:
                    if stop.outcome != "resource_limit":
                        raise
                    # stopped by this service's own watch: the model is too much for the job, and it has a name
                    await disk(_read_conversion, directory)
                    reason = "step_too_large"
                else:
                    reason = await disk(_read_conversion, directory)
            if reason == "timeout":
                raise _Stop("timeout", "deadline")
            if reason:
                await disk(_write_step_failure, job.root, part, reason)
                raise _Stop("failed", reason)
        job.progress = None

    async def execute(self, job: Job):
        started = time.monotonic()
        deadline = time.monotonic_ns() + job.budget_ns
        outcome, why = "failed", "unknown"
        try:
            await self._convert(job, deadline)
            await self._child(job, PACK_MODULE, {"root": str(job.root), "deadline": deadline}, deadline, progress=True)
            outcome, why = await disk(_read_result, job.root)
        except asyncio.CancelledError:
            outcome, why = "canceled", "cancel"
        except _Stop as stop:
            outcome, why = stop.outcome, stop.why
        except SpawnUnproven:
            self.uncertain = True
            outcome, why = "unavailable", "ownership_uncertain"
        except Exception as error:
            outcome, why = "failed", closed_why(type(error).__name__) or "unknown"
        finally:
            self.finish(job, outcome, why, time.monotonic() - started)

    async def stop(self):
        self.stopping.set()
        running = self.running
        if self.worker:
            self.worker.cancel()
        if running and running.task:
            running.task.cancel()
            await owned(asyncio.gather(running.task, return_exceptions=True))
        if self.worker:
            await asyncio.gather(self.worker, return_exceptions=True)
        for handler in list(self.handlers):
            if handler is not asyncio.current_task():
                handler.cancel()
        await asyncio.gather(*self.handlers, return_exceptions=True)
        if hasattr(self, "nc"):
            await self.nc.close()


async def main():
    config = json.loads(sys.stdin.buffer.readline(16385))
    service = Service(config)
    try:
        await service.start()
        await service.stopping.wait()
    finally:
        await service.stop()


if __name__ == "__main__":
    asyncio.run(main())
