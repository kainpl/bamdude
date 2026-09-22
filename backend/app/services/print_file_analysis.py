"""Per-print 3MF analysis shared by projection and inventory accounting.

The expensive part of a filament projection is parsing the embedded G-code.
That work is immutable for one archive revision, so it belongs to the live
print context rather than to every HTTP poller.  The worker deliberately has
no ORM, MQTT or FastAPI imports: it receives only a server-owned archive path
and a plate number.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pickle
import time
import zipfile
from bisect import bisect_right
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import FunctionType
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from backend.app.services.printer_manager import PrinterManager

logger = logging.getLogger(__name__)

AnalysisState = Literal["waiting_source", "empty", "preparing", "ready", "unavailable"]
LifecycleState = Literal["active", "finishing", "retired"]
_RETRY_DELAYS_SECONDS = (5.0, 30.0, 120.0)
_MAX_PENDING_CONTEXTS = 64
_MAX_GCODE_BYTES = 256 * 1024 * 1024
_MAX_LAYER_CHANNEL_ENTRIES = 1_000_000
_MAX_ANALYSIS_BYTES = 32 * 1024 * 1024
_MAX_RETAINED_ANALYSIS_BYTES = 256 * 1024 * 1024
_ANALYSIS_DEADLINE_SECONDS = 180.0


class AnalysisResourceError(RuntimeError):
    """The archive or its result exceeds a server-safety boundary."""


@dataclass(frozen=True)
class PrintFileAnalysis:
    """Immutable facts extracted from one 3MF plate.

    ``layer_usage`` deliberately retains the parser's cumulative millimetres:
    callers already derive grams from its fraction and slicer estimates.  Do
    not turn this into physical-spool attribution; that remains live data.
    """

    filament_usage: list[dict]
    layer_usage: dict[int, dict[int, float]] | None
    filament_properties: dict[int, dict]
    retained_bytes: int = 0
    layer_numbers: tuple[int, ...] = ()
    final_mm_by_filament: dict[int, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Build only the small lookup index; retain the canonical mm table."""
        if not self.layer_usage:
            return
        if not self.layer_numbers:
            object.__setattr__(self, "layer_numbers", tuple(sorted(self.layer_usage)))
        if not self.final_mm_by_filament:
            object.__setattr__(
                self,
                "final_mm_by_filament",
                dict(self.layer_usage[self.layer_numbers[-1]]),
            )

    def progress_fraction(self, filament_id: int, target_layer: int) -> float | None:
        """Return the same fraction as the legacy helper without re-scanning layers."""
        final = self.final_mm_by_filament.get(filament_id, 0.0)
        if final <= 0:
            return None
        index = bisect_right(self.layer_numbers, target_layer) - 1
        if index < 0:
            return None
        at_layer = (self.layer_usage or {})[self.layer_numbers[index]].get(filament_id, 0.0)
        return min(at_layer / final, 1.0)


@dataclass
class _Context:
    archive_id: int
    generation: int
    source: tuple[str, int | None, int, int] | None
    state: AnalysisState
    lifecycle: LifecycleState
    task: asyncio.Task[PrintFileAnalysis] | None = None
    analysis: PrintFileAnalysis | None = None
    error: str | None = None
    failed_attempts: int = 0
    retry_at: float | None = None


_executor: ProcessPoolExecutor | None = None


def _analysis_executor() -> ProcessPoolExecutor:
    """Use one bounded child across all printers, created only when needed."""
    global _executor
    if _executor is None:
        # ``spawn`` is the portable baseline (including Windows embedded
        # Python); no fork-only state can leak from the web server to a worker.
        _executor = ProcessPoolExecutor(max_workers=1)
    return _executor


async def _abandon_timed_out_executor(pool: ProcessPoolExecutor) -> None:
    """Kill/reap the single parser child after its hard deadline.

    ``ProcessPoolExecutor.shutdown`` only cancels queued futures; a parser
    stuck in ZIP decode keeps its child alive.  Current CPython keeps those
    child handles on the executor; isolate that implementation detail here and
    guard it for interpreter changes.
    """
    global _executor
    if _executor is pool:
        _executor = None
    processes = tuple(getattr(pool, "_processes", {}).values())
    pool.shutdown(wait=False, cancel_futures=True)

    def terminate_and_reap() -> None:
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            process.join(timeout=5)

    await asyncio.to_thread(terminate_and_reap)


def _parse_3mf(path_text: str, plate_id: int | None) -> PrintFileAnalysis:
    """Child-process entry point. Keep imports inside the child boundary."""
    from backend.app.utils.threemf_tools import (
        extract_filament_properties_from_3mf,
        extract_filament_usage_from_3mf,
        extract_layer_filament_usage_from_3mf,
    )

    path = Path(path_text)
    # The production source descriptor admits only a real regular file.  Some
    # legacy unit seams deliberately provide a virtual path and mock the
    # extractor below; do not turn that math fixture into a ZipFile contract.
    if path.is_file():
        _validate_gcode_size(path, plate_id)
    analysis = PrintFileAnalysis(
        filament_usage=extract_filament_usage_from_3mf(path, plate_id) or [],
        layer_usage=extract_layer_filament_usage_from_3mf(path, plate_id),
        filament_properties=extract_filament_properties_from_3mf(path) or {},
    )
    entries = sum(len(values) for values in (analysis.layer_usage or {}).values())
    if entries > _MAX_LAYER_CHANNEL_ENTRIES:
        raise AnalysisResourceError(f"layer timeline has {entries} entries (limit {_MAX_LAYER_CHANNEL_ENTRIES})")
    retained_bytes = len(pickle.dumps(analysis, protocol=pickle.HIGHEST_PROTOCOL))
    if retained_bytes > _MAX_ANALYSIS_BYTES:
        raise AnalysisResourceError(f"analysis result is {retained_bytes} bytes (limit {_MAX_ANALYSIS_BYTES})")
    return replace(analysis, retained_bytes=retained_bytes)


def _validate_gcode_size(path: Path, plate_id: int | None) -> None:
    """Reject a selected G-code member before any extractor inflates it."""
    try:
        with zipfile.ZipFile(path, "r") as archive:
            gcode_members = [name for name in archive.namelist() if name.endswith(".gcode")]
            if not gcode_members:
                return
            if plate_id is not None:
                wanted = f"plate_{int(plate_id)}.gcode"
                selected = next((name for name in gcode_members if name.endswith(wanted)), None)
            else:
                selected = None
            if selected is None:

                def _order(name: str) -> tuple[int, str]:
                    stem = name.rsplit("/", 1)[-1]
                    digits = "".join(char for char in stem if char.isdigit())
                    return (int(digits) if digits else 1 << 30, name)

                selected = min(gcode_members, key=_order)
            size = archive.getinfo(selected).file_size
    except (OSError, zipfile.BadZipFile) as exc:
        raise AnalysisResourceError("archive is not a readable 3MF") from exc
    if size > _MAX_GCODE_BYTES:
        raise AnalysisResourceError(f"G-code is {size} bytes (limit {_MAX_GCODE_BYTES})")


def _contexts(printer_manager: PrinterManager) -> dict[int, _Context]:
    contexts = getattr(printer_manager, "_print_file_analysis_contexts", None)
    if not isinstance(contexts, dict):
        contexts = {}
        printer_manager._print_file_analysis_contexts = contexts
    return contexts


def _finishing_contexts(printer_manager: PrinterManager) -> dict[tuple[int, int], _Context]:
    """Contexts held only while completion consumers finish their work."""
    contexts = getattr(printer_manager, "_print_file_analysis_finishing_contexts", None)
    if not isinstance(contexts, dict):
        contexts = {}
        printer_manager._print_file_analysis_finishing_contexts = contexts
    return contexts


def _all_contexts(printer_manager: PrinterManager) -> tuple[_Context, ...]:
    return tuple(_contexts(printer_manager).values()) + tuple(_finishing_contexts(printer_manager).values())


def _pending_context_count(printer_manager: PrinterManager) -> int:
    return sum(context.state == "preparing" for context in _all_contexts(printer_manager))


def _retained_analysis_bytes(printer_manager: PrinterManager) -> int:
    return sum(context.analysis.retained_bytes for context in _all_contexts(printer_manager) if context.analysis)


def _new_context(
    printer_manager: PrinterManager,
    archive_id: int,
    source: tuple[str, int | None, int, int] | None,
    *,
    lifecycle: LifecycleState = "active",
) -> _Context:
    """Create a new current-print generation owned by ``PrinterManager``."""
    generation = getattr(printer_manager, "_print_file_analysis_generation", 0) + 1
    printer_manager._print_file_analysis_generation = generation
    return _Context(
        archive_id=archive_id,
        generation=generation,
        source=source,
        state="empty" if source is not None else "waiting_source",
        lifecycle=lifecycle,
    )


def _retire(context: _Context) -> None:
    """Detach an obsolete context without publishing any later worker result."""
    context.lifecycle = "retired"
    if context.task is not None and not context.task.done():
        # This cancels our awaiter (and queued executor work when possible).
        # A process already executing parser code is bounded by the worker
        # supervisor work; it must never regain a manager-owned context here.
        context.task.cancel()


def _source(path: Path, plate_id: int | None) -> tuple[str, int | None, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    return (os.fspath(path), plate_id, stat.st_size, stat.st_mtime_ns)


def bind_print_file_analysis(
    printer_manager: PrinterManager,
    printer_id: int,
    archive_id: int,
    path: Path | None = None,
    plate_id: int | None = None,
) -> None:
    """Bind the manager's current-print context to an authoritative archive.

    Binding is deliberately cheap and does not parse anything.  It is called
    at print-start/adoption once the existing lifecycle has identified the
    archive.  A missing 3MF is a normal ``waiting_source`` state, not a cached
    failure.  The first projection or accounting consumer starts the parse
    after a source is available.
    """
    source = _source(path, plate_id) if path is not None else None
    contexts = _contexts(printer_manager)
    current = contexts.get(printer_id)
    if current is not None and current.archive_id == archive_id:
        if source is None or current.source == source:
            return
    if current is not None:
        _retire(current)
    contexts[printer_id] = _new_context(printer_manager, archive_id, source)


def notify_print_file_analysis_source_ready(
    printer_manager: PrinterManager,
    printer_id: int,
    archive_id: int,
    path: Path,
    plate_id: int | None,
) -> bool:
    """Attach a committed archive file to the already-bound current print.

    A late retry for an old archive must not resurrect a cache after the print
    has finished or replace a newer print.  Therefore this function only
    updates a matching active context and returns whether it did so.
    """
    source = _source(path, plate_id)
    if source is None:
        return False
    contexts = _contexts(printer_manager)
    current = contexts.get(printer_id)
    if current is None or current.archive_id != archive_id:
        return False
    if current.source != source:
        _retire(current)
        contexts[printer_id] = _new_context(printer_manager, archive_id, source)
    elif current.state == "unavailable":
        # A committed attach/retry is authoritative evidence that this source
        # may have changed even when its filesystem descriptor did not.  It
        # wakes the one bounded retry path; ordinary HTTP polls still honour
        # the backoff below.
        current.task = None
        current.error = None
        current.failed_attempts = 0
        current.retry_at = None
        current.state = "empty"
    return True


def begin_print_file_analysis_finishing(
    printer_manager: PrinterManager,
    printer_id: int,
    archive_id: int,
) -> bool:
    """Move the matching current print into its completion-only registry.

    Completion may overlap a very fast subsequent start.  Moving the old run
    first lets its accounting read the immutable table without ever replacing
    the new current-print context.  A stale/duplicate completion has no right
    to manufacture a context merely from a printer id, so it returns ``False``.
    """
    key = (printer_id, archive_id)
    finishing = _finishing_contexts(printer_manager)
    if key in finishing:
        return True

    contexts = _contexts(printer_manager)
    current = contexts.get(printer_id)
    if current is None or current.archive_id != archive_id:
        return False

    contexts.pop(printer_id, None)
    current.lifecycle = "finishing"
    finishing[key] = current
    return True


async def _prepare(
    context: _Context,
    path: Path,
    runner: Callable[[str, int | None], PrintFileAnalysis] | None,
    use_process: bool,
) -> PrintFileAnalysis:
    # ``get_print_file_analysis`` starts a task only after it has installed a
    # file-backed source.  Keep the proof beside the task boundary, where a
    # future lifecycle caller cannot accidentally submit a waiting context.
    source = context.source
    assert source is not None
    loop = asyncio.get_running_loop()
    if runner is not None:
        awaitable = asyncio.to_thread(runner, os.fspath(path), source[1])
        try:
            return await asyncio.wait_for(awaitable, timeout=_ANALYSIS_DEADLINE_SECONDS)
        except TimeoutError as exc:
            raise AnalysisResourceError("analysis exceeded its deadline") from exc
    if not use_process:
        awaitable = asyncio.to_thread(_parse_3mf, os.fspath(path), source[1])
        try:
            return await asyncio.wait_for(awaitable, timeout=_ANALYSIS_DEADLINE_SECONDS)
        except TimeoutError as exc:
            raise AnalysisResourceError("analysis exceeded its deadline") from exc
    pool = _analysis_executor()
    awaitable = loop.run_in_executor(pool, _parse_3mf, os.fspath(path), source[1])
    try:
        return await asyncio.wait_for(awaitable, timeout=_ANALYSIS_DEADLINE_SECONDS)
    except TimeoutError as exc:
        await _abandon_timed_out_executor(pool)
        raise AnalysisResourceError("analysis exceeded its deadline") from exc


async def get_print_file_analysis(
    printer_manager: PrinterManager,
    printer_id: int,
    archive_id: int,
    path: Path,
    plate_id: int | None,
    *,
    timeout: float | None = None,
) -> PrintFileAnalysis | None:
    """Return the one analysis for this printer/archive/source revision.

    A cancelled or timed-out caller never cancels the shared worker.  A source
    change replaces the context before the old task can publish into it.
    """
    contexts = _contexts(printer_manager)
    finishing = _finishing_contexts(printer_manager)
    active = contexts.get(printer_id)
    finishing_key = (printer_id, archive_id)
    context = active if active is not None and active.archive_id == archive_id else finishing.get(finishing_key)
    source = _source(path, plate_id)
    registry: dict[object, _Context]
    registry_key: object
    if context is not None:
        registry = finishing if context.lifecycle == "finishing" else contexts
        registry_key = finishing_key if context.lifecycle == "finishing" else printer_id
    elif active is None and source is not None:
        # Recovery safety net for a printing archive whose start bridge was
        # missed.  Never use it to replace an already-known newer print.
        context = _new_context(printer_manager, archive_id, source)
        contexts[printer_id] = context
        registry = contexts
        registry_key = printer_id
    else:
        return None

    if context.lifecycle == "retired":
        return None
    if source is None:
        # Retention/deletion after a completed cold parse cannot invalidate the
        # immutable bytes already held for this live or finishing run.  It only
        # prevents a new parse for a context that has none yet.
        return context.analysis
    if context.source != source:
        lifecycle = context.lifecycle
        _retire(context)
        context = _new_context(printer_manager, archive_id, source, lifecycle=lifecycle)
        registry[registry_key] = context

    if context.analysis is not None:
        return context.analysis
    if context.error is not None:
        retry_at = context.retry_at
        if retry_at is None or time.monotonic() < retry_at:
            return None
        context.error = None
        context.task = None
        context.retry_at = None
        context.state = "empty"
    if context.task is None:
        if _pending_context_count(printer_manager) >= _MAX_PENDING_CONTEXTS:
            context.error = "analysis admission is full"
            context.retry_at = time.monotonic() + _RETRY_DELAYS_SECONDS[0]
            context.state = "unavailable"
            logger.warning("3MF analysis admission full for printer %s archive %s", printer_id, archive_id)
            return None
        # Narrow test seam: production managers do not define this attribute;
        # tests can exercise the lifecycle without spawning a child process.
        candidate = getattr(printer_manager, "_print_file_analysis_runner", None)
        runner = candidate if isinstance(candidate, FunctionType) else None
        use_process = getattr(printer_manager, "_uses_print_file_analysis_process", False) is True
        context.task = asyncio.create_task(
            _prepare(context, path, runner, use_process), name=f"3mf-analysis-{printer_id}-{archive_id}"
        )
        context.state = "preparing"

    try:
        waiter = asyncio.shield(context.task)
        analysis = await (asyncio.wait_for(waiter, timeout) if timeout is not None else waiter)
    except TimeoutError:
        return None
    except asyncio.CancelledError:
        if context.lifecycle == "retired":
            return None
        raise
    except Exception as exc:  # worker crash, corrupt archive, resource failure
        if registry.get(registry_key) is context and context.lifecycle != "retired" and context.error is None:
            context.failed_attempts += 1
            context.error = str(exc)
            if context.failed_attempts <= len(_RETRY_DELAYS_SECONDS):
                context.retry_at = time.monotonic() + _RETRY_DELAYS_SECONDS[context.failed_attempts - 1]
            else:
                context.retry_at = None
            context.state = "unavailable"
            logger.warning(
                "3MF analysis unavailable for printer %s archive %s (attempt %s): %s",
                printer_id,
                archive_id,
                context.failed_attempts,
                exc,
            )
        return None

    # A newer print/source replaced this context while its child was running.
    if registry.get(registry_key) is not context or context.lifecycle == "retired":
        return None
    # A server-owned attach/replacement can race the child.  Never publish the
    # old bytes under the new descriptor; the next call owns a fresh task.
    current_source = _source(path, plate_id)
    if current_source != context.source:
        if current_source is not None:
            lifecycle = context.lifecycle
            _retire(context)
            registry[registry_key] = _new_context(printer_manager, archive_id, current_source, lifecycle=lifecycle)
        return None
    retained_after_publish = _retained_analysis_bytes(printer_manager) + analysis.retained_bytes
    if retained_after_publish > _MAX_RETAINED_ANALYSIS_BYTES:
        context.error = "analysis retained-memory budget is full"
        context.retry_at = time.monotonic() + _RETRY_DELAYS_SECONDS[0]
        context.state = "unavailable"
        logger.warning("3MF analysis retained-memory budget full for printer %s archive %s", printer_id, archive_id)
        return None
    context.analysis = analysis
    context.state = "ready"
    return analysis


def discard_print_file_analysis(printer_manager: PrinterManager, printer_id: int, archive_id: int) -> None:
    """Release only the context belonging to this completed archive."""
    key = (printer_id, archive_id)
    finishing = _finishing_contexts(printer_manager)
    context = finishing.pop(key, None)
    if context is None:
        context = _contexts(printer_manager).get(printer_id)
        if context is not None and context.archive_id == archive_id:
            _contexts(printer_manager).pop(printer_id, None)
        else:
            context = None
    if context is not None:
        _retire(context)


def discard_printer_print_file_analysis(printer_manager: PrinterManager, printer_id: int) -> None:
    """Release all transient analysis when the printer itself is deleted."""
    context = _contexts(printer_manager).pop(printer_id, None)
    if context is not None:
        _retire(context)
    finishing = _finishing_contexts(printer_manager)
    for key in [key for key in finishing if key[0] == printer_id]:
        _retire(finishing.pop(key))


def shutdown_print_file_analysis_workers() -> None:
    """Best-effort process cleanup for application shutdown and tests."""
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=True)
        _executor = None
