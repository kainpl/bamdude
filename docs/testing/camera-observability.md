# CAM-03 validation — 2026-09-13

## Camera Wall navigation regression — 2026-09-13 evening

Reproduced in the local Chrome/Vite session with `CAMERA_RUNTIME=worker`, two
live cameras and four snapshot cameras. The second Wall → Archive navigation
left all 48 thumbnail image elements pending; detached live subscriptions
remained on the backend. Separate auth-status requests still returned in
5–6 ms directly and 8–10 ms through Vite. Those timings do not measure browser
request queueing and are not a general API latency benchmark.

With explicit MJPEG image cancellation on React ref cleanup, three consecutive
Wall → Archive cycles loaded all 48 thumbnail image elements, and both live
subscriptions returned to zero at each exit. The backend was not restarted
between the failing and passing navigation checks. All six cameras rendered
between cycles and snapshot timestamps advanced. This is a six-camera local
acceptance check, not a 50-printer soak or an inline/worker performance A/B.

Automated checks exercise ref teardown on navigation, offline transition,
snapshot transition and Strict Mode replay; snapshot cancellation/retention;
fragmented UTF-8 worker logs, credential removal, oversized stderr and log rate
limits. A real supervised child capture verifies that session metrics and
startup/shutdown records reach the parent logger.

## Floating viewer and transport cap — 2026-09-13 late evening

Before the fix, Refresh → Close retained a detached MJPEG subscriber. The local
session accumulated four X2D subscribers despite one visible/closed popup; closing
the affected browser tab released them. The unmount effect had captured the first
image element, while refresh and minimize replaced or removed later elements.
The shared image-ref lifecycle now cancels each actual node, and StrictMode setup
restores its URL. The floating viewer is a single keyed selection; older persisted
arrays restore only their last valid entry.

Observed with the restarted worker backend and local Chrome/Vite:

- Refreshed X2D displayed a 1920px-wide frame; close detached to zero at 22:46:14.
- Minimize detached to zero at 22:49:02; relay ended with `viewers_gone` at 22:49:07.
- Selecting P1S replaced X2D at 22:50:59: one visible stream, P1S frame width 1280;
  X2D detached to zero and completed after its grace period.
- With wall maximum set to 8, UI detected `http/1.x` and displayed 2 live + 4
  snapshots. All six images decoded. A popup above the wall showed a snapshot;
  the DOM still contained exactly two live-stream images.
- Wall + popup → Archive: all 48 thumbnails loaded, none pending. Both live
  subscribers detached to zero and relays ended at 22:56:15.

Automated frontend run: **66 passed across 8 files**, including actual
PrintersPage persisted-list migration and card selection, popup refresh/minimize/
key replacement, shared slot release, HTTP/1/unknown cap, h2/h3 detection, mixed
protocol downgrade, and rejection of navigation/assets/cross-origin evidence.
HTTP/2 and HTTP/3 are covered with synthetic Resource Timing entries, not a live
reverse-proxy acceptance run. Budgets are per document, not cross-tab.

### Merge acceptance with the fixes branch

The integration checkout preserves the fixes branch's virtual grids, saved-sort
updates and live-status prioritization alongside the camera changes. The combined
run passed **127 tests across 13 files** (camera, Printers, Queue, WebSocket,
status batching and progressive-list checks). Production build, including both
TypeScript projects, passed. Backend source is identical to the tested camera
branch; no backend changes were introduced while resolving the merge.

## Earlier instrumentation baseline

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
