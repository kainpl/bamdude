# Camera timing and frame provenance

[Українською](camera-observability.uk.md)

## Browser live-camera limit and floating window

The browser measures the protocol of completed same-origin `/api/` requests
using Resource Timing `nextHopProtocol`. HTTP/1.x and unavailable/empty protocol
information keep a conservative **two live MJPEG requests per tab**. Confirmed
`h2` or `h3` permits the selected wall limit, within the existing maximum of 16.
HTTPS alone and the HTML document's protocol are not sufficient evidence. If
HTTP/1.x is observed after HTTP/2 or HTTP/3, the tab stays at two until reload.
See the [Resource Timing specification](https://www.w3.org/TR/resource-timing/#dom-performanceresourcetiming-nexthopprotocol).

The wall (including kiosk mode) and floating camera share the same tab budget.
Additional visible cameras use cancellable snapshots; their shared queue has at
most two requests. The wall's saved maximum is preserved; the displayed effective
count respects the transport limit. A notice explains when the budget reduces
live viewing. This reduces browser connection starvation; it does not measure
server capacity or coordinate separate tabs/windows sharing an origin.

Only one floating camera opens on the Printers page. Clicking another printer's
camera replaces it, cancelling the previous image request and reconnect/stall
timers. Old saved lists restore only their last valid camera. Minimize stops the
live request; expand reacquires a slot. Refresh and close cancel the actual image
node, including replacements after refresh and React Strict Mode reattachment.
Backend fan-out releases the viewer; a shared producer remains while other
viewers still need it. This applies to both inline and worker runtime because the
browser HTTP lifecycle is common to them.

Camera status now includes optional `telemetry` (the current or last completed live
producer) and `last_snapshot` (the last HTTP snapshot decision). Existing fields,
permissions and clients remain compatible. Reading
`GET /api/v1/printers/{id}/camera/status` does not open a camera or run FFprobe.

| Evidence | Meaning |
| --- | --- |
| `session_id`, `attempt_id` | One live producer; one physical capture/connect attempt. Reconnects get a new attempt suffix. |
| `first_frame_ms` | Monotonic time from the attempt's start to its first complete JPEG markers, before cleanup. It is not HTTP response time or a decoder integrity check. |
| `caller_wait_ms` | That caller's total wait, including waiting for a shared producer and its cleanup. Present on capture results, snapshot evidence and first-frame diagnosis. |
| `cleanup_ms` | Observed owner teardown time. Currently measured for `CameraAttempt` and live chamber streams; other paths may return null. |
| `frame_age_ms` | Age of the last observed frame, not printer telemetry age. |
| `attempts_total`, `reconnects_total` | Producer attempts and reconnects. Scheduled HTTP snapshot polls are attempts, not reconnects. |
| `consecutive_failures` | Built-in RTSP resets after its existing stable window; external reconnect paths count consecutive attempts without frames, snapshot polling counts failed polls. These policies are not identical. |
| `output_frames`, `output_fps` | Frames published by this producer; FPS uses up to 64 samples within the last 10 seconds. Neither is source camera FPS. |
| `subscriber_dropped_frames` | JPEGs rejected because slow local subscribers' queues are full, summed across subscribers. Not network packet loss. |
| `source_codec`, `source_resolution` | Optional observations from the existing bounded FFmpeg stderr drain. No extra pipe reader or probe; null means unknown. |
| `started_at`, `active`, `end_reason` | UTC correlation, whether the producer is still active, and a fixed reason such as `client_disconnected`, `connect_failed`, `retry_exhausted` or `cleanup_failed`. |

`last_snapshot.frame_source` is `own_capture`, `shared_capture`, `live_buffer`,
`snapshot_cache`, or null on failure. Cache/buffer delivery has no fabricated
connect time. Shared callers retain their producer's attempt ID and first-frame
time, but get separate wait times. A follower that captures after a failed leader
reports its own attempt. A caller timing out before the producer completes has
no completed attempt evidence. The bytes-only internal capture APIs still work.

Diagnosis adds timing fields to its first-frame stage; its existing `duration_ms`
still means the stage's total elapsed time. Existing UI clients may ignore the
additional JSON fields; there is no new dashboard in this change.

Each producer writes one `Camera session completed` INFO record to the backend
log, after cleanup, with the same timing/counter fields. This record is included
when downloading backend logs; it is not a new support-bundle attachment.
No per-frame logs, JPEGs, URLs or access codes are retained by the metrics module.
Records for active producers plus at most 128 completed identities and 128
snapshot decisions are held in RAM. Deleting a printer clears its association;
restart clears all evidence. A missing record is unknown, not proof of success.

For support, save camera status while the issue occurs, run diagnosis only when
appropriate (it can capture if no live producer exists), and download the current
backend log. Match `attempt_id` before comparing durations. Output queue drops
alone do not identify a Wi-Fi failure or explain API/WebSocket latency.

## Experimental worker runtime

`CAMERA_RUNTIME=worker` is opt-in; `inline` remains the default. It starts one
supervised child before camera work begins. One-shot built-in/external captures,
built-in Bambu chamber/RTSPS live view, and external live MJPEG, RTSP and
snapshot sources use its authenticated loopback JPEG relay, while this process
keeps the existing browser fan-out. Each worker live lease has one producer per
stable, secret-free printer identity and a latest-frame queue. There are at most
64 relays and a frame is capped at 2 MiB, bounding queued live JPEG data to
128 MiB per process; a lost media socket releases the producer.

The mode fails closed. A worker bootstrap/containment failure does not fall back
to inline transport. Built-in Bambu chamber/RTSPS sources and external live
sources use worker-owned decoded JPEG leases. Built-in RTSPS applies the same
per-model probe and reconnect profile as inline live view. Virtual Printer camera passthrough
is a separate raw TCP lease, so it stays byte-for-byte and cannot overlap a
JPEG relay for the same source. Test this setting first on a supported host; the
current validation does not replace a physical-farm or Linux-service acceptance
run.

[Validation and synthetic baseline](testing/camera-observability.md).

### Worker logs and browser cleanup

After changing `CAMERA_RUNTIME`, restart the backend. `Camera worker ready`
records its PID and generation; `Camera worker stopped` records its exit code.
Worker transport logs and completed-session metrics reach `bamdude.log` with a
`Camera worker [module]` prefix. Camera URLs and task-scoped credentials are
removed before transmission; exceptions keep their type and source locations,
without their raw text or local variables. Records are limited to 8 KiB and
200 per 10 seconds. Rate-limit summaries report dropped records. Unstructured
stderr is counted and reported without copying its potentially sensitive text.

At INFO, each live relay logs its start (printer ID, session ID, source identity,
protocol and requested FPS), first-frame latency once, and an end summary with
frame count, duration and reason. These count frames delivered to the backend
relay, not proof of browser rendering. Worker transport records carry the same
source identity, so they can be matched to a printer without its address or
credentials. Viewer attach/detach records include the current subscriber count.

Camera Wall explicitly cancels each MJPEG image before it is detached or
replaced. Removing the DOM element alone can leave Chromium downloading the
stream and occupying HTTP/1.1 connections after navigation. This cleanup is
shared by the inline and worker modes. Snapshot tiles retain the last decoded
image, show its receive time at the bottom right, share two request slots, and
cancel pending work on navigation; a 20-second deadline includes the JPEG body.
