# CAM-03 validation — 2026-09-13

Base: `f8f67862`, branch `feature/camera-observability-worker-plan`.
Windows local checkout, Python from the project's existing venv.

Executed: Ruff over `backend/` and the benchmark script; all camera/FFmpeg tests
plus `backend/tests/integration/test_printers_api.py`: **432 passed, 142.10 s**.
New tests cover shared IDs/timings, failed-leader recovery, first-frame timing
before cleanup, slow-viewer drops, bounded retention/deletion, read-only status,
ContextVar isolation across generator yields, failed spawn and cancellation.
The broader set covers rotation, TLS cleanup, stdout/stderr ownership, backoff,
external cameras, API permissions and printer deletion. No frontend code changed.

Reproduce in PowerShell (replace Python path with your venv):

```powershell
$cameraTests = @(rg --files backend/tests | Where-Object { $_ -match '(camera|ffmpeg)' })
python -m pytest @cameraTests backend/tests/integration/test_printers_api.py -q
python -m ruff check backend/ scripts/benchmark_camera_metrics.py
python -m scripts.benchmark_camera_metrics
```

The benchmark alternates seven baseline and seven instrumented runs: 50
concurrent synthetic producers, 400 frames each, 8 KiB payload, one scheduling
yield per frame. No camera/network, FFmpeg, disk or log handler. Completion INFO
logging is disabled so this measures instrumentation and registry overhead;
production logging/storage cost must be measured separately.

| Median | Baseline | Instrumented |
| --- | ---: | ---: |
| Wall time / 20,000 frames | 30.102 ms | 57.042 ms |
| Process CPU time | 31.250 ms | 62.500 ms |
| Probe scheduling p99 | 0.120 ms | 0.300 ms |

Additional wall time: **1.347 microseconds/frame** on this run. CPU time has
coarse Windows accounting granularity; these numbers are not a throughput
promise. The benchmark asserts complete output, no active record leak, and the
completed-registry cap. Unit tests exercise eviction over multiple identities.

This is a telemetry-overhead baseline, **not a main API or worker A/B baseline**.
No real printer, production GPU, farm Wi-Fi or cloud portal was exercised.
The worker plan requires a separate reproducible API/MQTT/WS/media A/B before
rollout. This change does not claim to resolve all farm latency.

## Worker-runtime relay validation — 2026-09-13

The experimental `CAMERA_RUNTIME=worker` path was verified in the dedicated
Windows worktree with the project's existing Python environment. The child
authenticates with a one-use loopback bootstrap, runs inside a Windows Job
Object, relays bounded JPEG frames on the separate media listener, preserves
control responsiveness during concurrent capture, and releases a live producer
when its media relay closes. The live HTTP adapter retains the existing MJPEG
response and `MjpegBroadcaster` fan-out while the physical source runs in the
child. It supports external sources and built-in Bambu chamber/RTSPS sources;
worker status exposes the real source type, so snapshot and background consumers
do not open a second camera reader. Built-in RTSPS receives the same per-model
FFmpeg probe and reconnect profile as the inline transport.

The relay permits at most 64 active media queues and drops JPEGs over 2 MiB.
Each process therefore retains at most 128 MiB of queued live frames. These are
backpressure limits, not a promise that a 50-camera farm needs that much memory.

Completed bounded commands and results for the worker rollout:

| Scope | Result |
| --- | --- |
| Camera services, worker protocol/containment/relay, FFmpeg drain | 193 passed |
| Camera API units, status/fan-out invariants, Virtual Printer startup/proxy | 242 passed |
| Cloud Link, Obico, layer timelapse and finish-photo consumers | 156 passed |
| Bot camera controls and integration camera API | 49 passed |
| Focused profile/status/backpressure checks before the broad run | 103 passed |
| Ruff on changed worker, camera and VP files | passed |

The broad combined command is intentionally split because this Windows shell
can interrupt a long pytest process before it prints a completion line. Each
group above printed its own passing summary. Graphify must still be refreshed
before a delivery merge.

Not covered by automated tests: a physical Bambu chamber/RTSP live source,
hardware decoder profiles, a 50-camera soak with API/MQTT/WebSocket latency
measurements, and Linux service/cgroup/watchdog operation. Virtual Printer raw
TCP passthrough now has a worker-owned, byte-for-byte lease with a loopback
echo acceptance test. Built-in Bambu live view is worker-owned too; its HTTP
adapter and profile handoff are covered with local tests, while a physical
printer acceptance run remains required.

Українською: перевірено 432 тести камер/FFmpeg та API принтерів. Синтетичний
прогін вимірює лише накладні витрати метрик без реальних камер, декодування та
запису логів. Це не польове приймання й не доказ усунення пауз API/WebSocket.
