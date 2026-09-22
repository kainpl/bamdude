"""Measure one synthetic large-3MF analysis without touching a farm or database.

Run from repository root, serially:

    .\\venv\\Scripts\\python.exe -m scripts.benchmark_print_file_analysis

The generated archive has a deterministic 70 MiB embedded G-code plate.  The
numbers describe this machine and interpreter only; they are not a farm SLA.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import tempfile
import time
import zipfile
from pathlib import Path

import psutil

from backend.app.services import print_file_analysis
from backend.app.services.print_file_analysis import (
    get_print_file_analysis,
    shutdown_print_file_analysis_workers,
)
from backend.app.services.printer_manager import PrinterManager

GCODE_BYTES = 70 * 1024 * 1024
WAITERS = 100


def _write_fixture(path: Path) -> int:
    """Write one valid, multi-marker G-code plate without retaining it in RAM."""
    metadata = (
        b'<config><plate><metadata key="index" value="1"/>'
        b'<metadata key="prediction" value="900"/>'
        b'<metadata key="weight" value="12.5"/>'
        b'<filament id="1" type="PLA" color="#FFFFFF" used_g="12.5"/>'
        b"</plate></config>"
    )
    line = b"G1 X10 Y10 E0.01000 F1200 ;" + (b"x" * 480) + b"\n"
    written = 0
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Metadata/slice_info.config", metadata)
        with archive.open("Metadata/plate_1.gcode", "w") as gcode:
            while written < GCODE_BYTES:
                layer = written // (256 * 1024)
                marker = f"M73 L{layer}\nM620 S0\n".encode()
                gcode.write(marker)
                written += len(marker)
                chunk = min(GCODE_BYTES - written, 256 * 1024)
                repeats, remainder = divmod(chunk, len(line))
                if repeats:
                    gcode.write(line * repeats)
                    written += repeats * len(line)
                if remainder:
                    gcode.write(line[:remainder])
                    written += remainder
        return archive.getinfo("Metadata/plate_1.gcode").file_size


async def _measure(path: Path) -> dict[str, float | int | None]:
    manager = PrinterManager()
    loop_gaps_ms: list[float] = []
    measuring = True

    async def probe() -> None:
        while measuring:
            started = time.perf_counter()
            await asyncio.sleep(0.01)
            loop_gaps_ms.append(max((time.perf_counter() - started) * 1000 - 10.0, 0.0))

    ticker = asyncio.create_task(probe())
    started = time.perf_counter()
    try:
        results = await asyncio.gather(*(get_print_file_analysis(manager, 1, 1, path, 1) for _ in range(WAITERS)))
    finally:
        measuring = False
        await ticker
    assert results[0] is not None
    assert all(result is results[0] for result in results)
    process = print_file_analysis._executor
    child_rss = None
    for child in tuple(getattr(process, "_processes", {}).values()):
        if child.pid:
            child_rss = psutil.Process(child.pid).memory_info().rss
            break
    ordered = sorted(loop_gaps_ms)
    return {
        "cold_plus_100_waiters_seconds": round(time.perf_counter() - started, 3),
        "loop_gap_max_ms": round(max(loop_gaps_ms, default=0.0), 3),
        "loop_gap_p95_ms": round(statistics.quantiles(ordered, n=20)[18] if len(ordered) >= 20 else 0.0, 3),
        "child_rss_bytes_after_parse": child_rss,
        "retained_analysis_bytes": results[0].retained_bytes,
    }


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="bamdude-3mf-analysis-") as directory:
        path = Path(directory) / "synthetic.gcode.3mf"
        gcode_size = _write_fixture(path)
        try:
            result = await _measure(path)
        finally:
            shutdown_print_file_analysis_workers()
    print(
        json.dumps(
            {
                "gcode_uncompressed_bytes": gcode_size,
                "waiters": WAITERS,
                "single_flight_expected_parses": 1,
                "result": result,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
