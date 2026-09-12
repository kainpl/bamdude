# Fleet bootstrap validation

Software acceptance, 2026-09-13, using production modules and synthetic printer
states. No farm printer was contacted. This change fixes reproduced failure
modes; it does not establish the cause of all pauses on the reporting farm.

## Contract and implementation

Printer cards observe the same `printerStatus` query cache populated by
`useWebSocket`. They do not wait for a REST status response before using that
data. The printer configuration list and application JavaScript still have to
load before the page can render its cards.

`api/printerStatusBatch.ts` combines simultaneous status reads into one HTTP
request for 50 printers, preserving single reads for a detail view. Batches are
limited to 100 IDs and larger groups run sequentially. Each HTTP request has a
15-second deadline. REST remains useful for archive/plate enrichment and for a
user without WebSocket permission. In-flight live patches keep a late REST
response from overwriting newer WS fields; there is no second persistent cache.
Returning to a visible tab refetches only active queries.

The authenticated `/printers/status/batch` route uses the existing single-status
serializer. Archive IDs and completed queue-row existence are fetched in bounded
queries, with the same subtask/open-archive precedence. Missing IDs are omitted
and fail only their own callers. No schema or permission is added.

`ConnectionManager` gives each viewer a FIFO and one writer. Broadcast producers enqueue
without waiting for a browser's network send. Direct replies, initial statuses,
live statuses, print/queue events and pong share that writer. The queue is bounded
by 256 pending messages and 4 MiB, with a 5-second send deadline. Overflow/timeout
closes the affected client with 1013; reconnection obtains a fresh snapshot.
Transient events are not replayed across disconnects. Cloud Link's synchronous
enqueue-only tap and per-user audience rules remain intact. All writers are
owned by disconnect/shutdown, including eviction and cancellation before startup.
Only the connection's own direct-response/bootstrap producer waits for its queue
to drain when it grows, keeping large initial snapshots inside the bounds. Global
and per-user broadcast producers never wait on a viewer. This wait preserves FIFO
order and is released on writer progress, failure or disconnect.

WebSocket upgrade verifies the token and resolves its owner in one database
query. Existing token mint permissions and terminal authentication rejection
remain in effect. A deleted token owner is rejected.

## Diagnostics available from backend logs

The existing individual initial `printer_status` messages are followed by an
additive `initial_status_complete` marker. New browsers flush these states into
the query cache and acknowledge once with `initial_status_applied`. Old browsers
can ignore the marker and still receive their statuses.

- `WebSocket bootstrap timing`: auth/accept, initial enqueue, printer count, ID.
- `WebSocket bootstrap applied`: matching ID, server elapsed time including the
  acknowledgement's return trip, and browser duration starting before ws-token
  mint. This means cache-ready, **not first DOM paint** or fresh MQTT telemetry.
- Slow send, send timeout and outbox overflow warnings isolate stalled viewers.

No tokens or printer payloads are logged by these additions. Missing ACK can
also mean an older client, closed tab or interrupted connection. These timings
cannot by themselves split HTTP token latency into browser, proxy and server
phases, or measure JavaScript download and printer-list loading.

## Regressions exercised

- The slow-first-viewer regression failed against the previous manager: a
  healthy viewer did not receive its update before the deadline. It passes with
  the per-client writer. Producers do not wait for the stalled viewer.
- FIFO ordering for a 50-printer snapshot, its marker, print completion, live
  update and pong; message/byte overflow, hung send, hung close, disconnect before
  writer startup, owner filtering and Cloud Link listener behavior.
- Actual ASGI bootstrap for both 50 and 300 printers. The 300-printer case exposed
  self-overflow while preparing a large initial snapshot; pacing the connection's
  snapshot producer fixes it without blocking other viewers or broadcast producers.
- The real React printer page displays 50 live job names while its sole batch
  response remains deliberately unresolved. Separately, the WS hook consumes
  50 messages and the marker, writes cache data despite pending queries and
  sends the acknowledgement. These component tests use jsdom, not a physical
  browser's paint timings.
- REST batch parity for 50 printers on SQLite and a freshly started disposable
  bundled PostgreSQL, including cloud subtask precedence and completed plate
  holds. At most five SQL statements serve the batch. PostgreSQL starts on a
  temporary data directory and free loopback port, and stops in fixture cleanup.
- Missing/offline printers, batch bounds and authentication; failed reads and
  recovery; duplicate callers; sequential larger batches; late REST vs live
  progress/state and absent Wi-Fi measurements.

## Verification commands

```text
python -m pytest backend/tests/ -n 2 -q --tb=short
python -m pytest backend/tests/integration/test_printer_status_batch_postgres.py -q
ruff check backend/
npm run lint
npm run typecheck
npm run test:run -- --maxWorkers=2
npm run build
graphify update .
```

Frontend commands run from `frontend/`; Python uses the configured development
venv. The documentation repository also runs `mkdocs build --strict`.

| Check | Result |
| --- | --- |
| Full backend run | 13,350 passed, 153 skipped, one test-fixture failure subsequently fixed and retested |
| Frontend suite, two workers | 312 files passed; 3,350 tests passed, one existing skip |
| Final backend tests for delivery, bootstrap, auth, REST batch, PostgreSQL and Cloud Link | 96 passed |
| Disposable PostgreSQL fleet parity | Passed |
| Ruff, frontend lint and typecheck | Passed; two existing hook-dependency lint warnings |
| Production frontend build | Passed; existing large-chunk warning |
| EN/UK documentation, strict MkDocs build | Passed |
| Graphify AST update | Completed |

The full backend workers had already imported the new PostgreSQL fixture before
its UTC timestamp was corrected to the archive model's naive-UTC convention.
Its only failure was inserting that fixture, before exercising the status query.
The corrected fixture passed separately and in the final 96-test selection above.
That final selection also covers the large-snapshot pacing and shutdown regression
added while the broad run was in progress. The full suite was not rerun after
those targeted corrections; no remaining failure is being hidden as a skip.

The first frontend run alongside other checks hit timeouts in four files; those
files passed in isolation and the entire suite then passed with two workers.
The initial eight-worker backend launch encountered a Windows WMI failure during
worker startup; the full run was restarted with two workers. The pre-commit
TypeScript hook initially selected Windows' WSL launcher without a Linux bash;
Git Bash was put first in PATH, keeping the hook enabled.

The final pre-commit run passed with every applicable hook enabled.

## Farm follow-up

Install the build, record when the page is opened and collect backend logs over
that interval. Correlate timing/applied by ID and check slow-send warnings and
existing slow-request timing. Keep the finish-photo setting recorded with the
measurement. A cache-ready ACK that is fast while the page remains slow directs
the next investigation toward configuration loading, JavaScript and rendering;
a slow ACK still needs the network/host context. Local emulation cannot certify
the farm's Wi-Fi, workstation load or camera processes.
