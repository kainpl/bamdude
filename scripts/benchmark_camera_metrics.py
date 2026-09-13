"""Synthetic CAM-03 overhead, no camera/network/FFmpeg. Run from repository root.

python -m scripts.benchmark_camera_metrics
Alternates baseline/instrumented runs; numbers are local, not a farm benchmark.
"""

import asyncio
import json
import logging
import statistics
import time

from backend.app.services import camera_metrics as metrics

JPEG = b"\xff\xd8" + bytes(8192) + b"\xff\xd9"
PRODUCERS = 50
FRAMES = 400


async def run(observed):
    async def source(printer_id):
        if observed:
            metrics.current.get().begin_attempt()
        for _ in range(FRAMES):
            await asyncio.sleep(0)
            yield metrics.record_frame(JPEG) if observed else JPEG

    factory = metrics.observed_stream("synthetic")(source) if observed else source

    async def consume(printer_id):
        count = 0
        async for frame in factory(printer_id):
            assert frame is JPEG
            count += 1
        assert count == FRAMES

    latencies = []
    finished = False

    async def probe():
        while not finished:
            started = time.perf_counter()
            await asyncio.sleep(0)
            latencies.append((time.perf_counter() - started) * 1000)

    ticker = asyncio.create_task(probe())
    cpu, wall = time.process_time(), time.perf_counter()
    await asyncio.gather(*(consume(i) for i in range(PRODUCERS)))
    finished = True
    await ticker
    assert not metrics._active
    assert len(metrics._completed) <= 128
    return {
        "wall_ms": (time.perf_counter() - wall) * 1000,
        "cpu_ms": (time.process_time() - cpu) * 1000,
        "probe_p99_ms": sorted(latencies)[int(len(latencies) * 0.99)],
    }


async def main():
    logging.getLogger(metrics.__name__).setLevel(logging.WARNING)
    results = {False: [], True: []}
    for _ in range(7):
        for observed in (False, True):
            results[observed].append(await run(observed))
    output = {"producers": PRODUCERS, "frames_per_run": PRODUCERS * FRAMES, "runs_each": 7}
    for observed, name in ((False, "baseline"), (True, "instrumented")):
        output[name] = {
            key: round(statistics.median(r[key] for r in results[observed]), 3) for key in results[observed][0]
        }
    output["wall_overhead_us_per_frame"] = round(
        (output["instrumented"]["wall_ms"] - output["baseline"]["wall_ms"]) * 1000 / (PRODUCERS * FRAMES), 3
    )
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
