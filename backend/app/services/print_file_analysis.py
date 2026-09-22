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
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import FunctionType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.app.services.printer_manager import PrinterManager

logger = logging.getLogger(__name__)


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


@dataclass
class _Context:
    archive_id: int
    source: tuple[str, int | None, int, int]
    task: asyncio.Task[PrintFileAnalysis] | None = None
    analysis: PrintFileAnalysis | None = None
    error: str | None = None


_executor: ProcessPoolExecutor | None = None


def _analysis_executor() -> ProcessPoolExecutor:
    """Use one bounded child across all printers, created only when needed."""
    global _executor
    if _executor is None:
        # ``spawn`` is the portable baseline (including Windows embedded
        # Python); no fork-only state can leak from the web server to a worker.
        _executor = ProcessPoolExecutor(max_workers=1)
    return _executor


def _parse_3mf(path_text: str, plate_id: int | None) -> PrintFileAnalysis:
    """Child-process entry point. Keep imports inside the child boundary."""
    from backend.app.utils.threemf_tools import (
        extract_filament_properties_from_3mf,
        extract_filament_usage_from_3mf,
        extract_layer_filament_usage_from_3mf,
    )

    path = Path(path_text)
    return PrintFileAnalysis(
        filament_usage=extract_filament_usage_from_3mf(path, plate_id) or [],
        layer_usage=extract_layer_filament_usage_from_3mf(path, plate_id),
        filament_properties=extract_filament_properties_from_3mf(path) or {},
    )


def _contexts(printer_manager: PrinterManager) -> dict[int, _Context]:
    contexts = getattr(printer_manager, "_print_file_analysis_contexts", None)
    if not isinstance(contexts, dict):
        contexts = {}
        printer_manager._print_file_analysis_contexts = contexts
    return contexts


def _source(path: Path, plate_id: int | None) -> tuple[str, int | None, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    return (os.fspath(path), plate_id, stat.st_size, stat.st_mtime_ns)


async def _prepare(
    context: _Context,
    path: Path,
    runner: Callable[[str, int | None], PrintFileAnalysis] | None,
    use_process: bool,
) -> PrintFileAnalysis:
    loop = asyncio.get_running_loop()
    if runner is not None:
        return await asyncio.to_thread(runner, os.fspath(path), context.source[1])
    if not use_process:
        return await asyncio.to_thread(_parse_3mf, os.fspath(path), context.source[1])
    return await loop.run_in_executor(_analysis_executor(), _parse_3mf, os.fspath(path), context.source[1])


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
    source = _source(path, plate_id)
    if source is None:
        return None

    contexts = _contexts(printer_manager)
    context = contexts.get(printer_id)
    if context is None or context.archive_id != archive_id or context.source != source:
        context = _Context(archive_id=archive_id, source=source)
        contexts[printer_id] = context

    if context.analysis is not None:
        return context.analysis
    if context.error is not None:
        return None
    if context.task is None:
        # Narrow test seam: production managers do not define this attribute;
        # tests can exercise the lifecycle without spawning a child process.
        candidate = getattr(printer_manager, "_print_file_analysis_runner", None)
        runner = candidate if isinstance(candidate, FunctionType) else None
        use_process = getattr(printer_manager, "_uses_print_file_analysis_process", False) is True
        context.task = asyncio.create_task(
            _prepare(context, path, runner, use_process), name=f"3mf-analysis-{printer_id}-{archive_id}"
        )

    try:
        waiter = asyncio.shield(context.task)
        analysis = await (asyncio.wait_for(waiter, timeout) if timeout is not None else waiter)
    except TimeoutError:
        return None
    except Exception as exc:  # worker crash, corrupt archive, resource failure
        context.error = str(exc)
        logger.warning("3MF analysis unavailable for printer %s archive %s: %s", printer_id, archive_id, exc)
        return None

    # A newer print/source replaced this context while its child was running.
    if contexts.get(printer_id) is not context:
        return None
    # A server-owned attach/replacement can race the child.  Never publish the
    # old bytes under the new descriptor; the next call owns a fresh task.
    current_source = _source(path, plate_id)
    if current_source != context.source:
        if current_source is not None:
            contexts[printer_id] = _Context(archive_id=archive_id, source=current_source)
        return None
    context.analysis = analysis
    return analysis


def discard_print_file_analysis(printer_manager: PrinterManager, printer_id: int, archive_id: int) -> None:
    """Release only the context belonging to this completed archive."""
    context = _contexts(printer_manager).get(printer_id)
    if context is not None and context.archive_id == archive_id:
        _contexts(printer_manager).pop(printer_id, None)


def shutdown_print_file_analysis_workers() -> None:
    """Best-effort process cleanup for application shutdown and tests."""
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=True)
        _executor = None
