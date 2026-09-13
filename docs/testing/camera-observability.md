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
when its media relay closes. The external live HTTP adapter retains the existing
MJPEG response and `MjpegBroadcaster` fan-out while the physical external source
runs in the child; the test uses a local snapshot server and verifies a frame
reaches the client and snapshot cache.

Completed commands and results:

| Scope | Result |
| --- | --- |
| Worker protocol, containment, capture/live relay, runtime adapter, external camera | 238 passed |
| Camera routes, fan-out, profiles, TLS, capability/status and worker HTTP adapter | 90 passed |
| Cloud Link snapshot | 35 passed |
| Obico | 48 passed |
| Layer timelapse | 16 passed |
| Finish photo moment + bot camera controls | 39 passed |
| Integration camera API | 35 passed |
| Virtual Printer startup/proxy safety | 170 passed |
| Ruff on changed worker, camera and VP files | passed |

The broad combined command was not recorded as a pass because this shell
environment stopped it around its time budget without a pytest completion line;
the same coverage was therefore rerun in the bounded groups above. Graphify AST
update was started after the code changes; its local CLI leaves a zero-CPU child
after extraction in this Windows host, so it must be checked again before a
delivery merge.

Not covered by automated tests: a physical Bambu chamber/RTSP live source,
hardware decoder profiles, a 50-camera soak with API/MQTT/WebSocket latency
measurements, Linux service/cgroup/watchdog operation, and the future raw TCP
lease for Virtual Printer. Worker mode rejects built-in Bambu live view and
Virtual Printer raw passthrough instead of silently creating a second camera
owner.

Українською: перевірено 432 тести камер/FFmpeg та API принтерів. Синтетичний
прогін вимірює лише накладні витрати метрик без реальних камер, декодування та
запису логів. Це не польове приймання й не доказ усунення пауз API/WebSocket.
