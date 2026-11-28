# CLAUDE.md

Guidance for coding agents working in this repository. Claude Code reads this
file on its own; `AGENTS.md` points other tools here. It covers how the code is
built, what must not be broken and how a change is done end to end. A
maintainer's personal context lives in `CLAUDE.local.md`, which is not in the
repository.

## Project

BamDude — self-hosted print archive + farm management for Bambu Lab. Hard fork of Bambuddy. Python 3.12 / FastAPI · React 19 / TS / Tailwind 4 · SQLite or PostgreSQL · MQTT · aiogram 3.x. en+uk locales.

## Knowledge base

The public record of the design is: the Key Invariants below (short form) with
their full text in [`docs/development/invariants/`](docs/development/invariants/README.md),
each migration's docstring, `CONTRIBUTING.md`, and `CHANGELOG.md` (prose: what
changed and why). Architecture rationale and rejected alternatives also live in
the maintainer's private Obsidian vault; `Vault:` pointers are paths inside it.

Every release attaches a code graph `bamdude-code-graph-<version>.json.gz` (plus
a `.report.md`). With [graphify](https://pypi.org/project/graphifyy/) installed,
gunzip it to `graphify-out/graph.json` and `graphify query "<question>"` returns
a scoped subgraph; `graphify update .` refreshes it after your edits.

## Local research and acceptance artifacts

Probes, review reports, logs, screenshots and evidence bundles go outside the
checkout, in `../research-artifacts/<task>-<date>/`; never inside a checkout,
never staged. Follow-ups preserve existing evidence and checksums; durable
findings go to the vault, reusable regression tests to the repo's test dirs.
`SHA256SUMS` is UTF-8 without BOM with LF endings (so `sha256sum -c` reads it);
never rewrite a sealed manifest. Anything regenerable — pytest `--basetemp`, run
scratch, Python/PostgreSQL runtimes, tree copies and exports, built sites,
database copies — goes to `../research-artifacts/_runtime/<task>/`, never into
the evidence folder or a manifest, and is deleted when the task closes.

## Commands

```bash
node scripts/set_version.js 0.X.Y                # bump backend+frontend+pyproject
DEBUG=true uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000 --loop asyncio --timeout-graceful-shutdown 15
cd frontend && npm run dev                       # /api → :8000
ruff check backend/                              # +--fix; ruff format
cd frontend && npm run lint && npm run typecheck # ⚠ NOT `npx tsc --noEmit` — that checks NOTHING
pytest backend/tests/ -n 4 --timeout=300 --timeout-method=thread
pytest backend/tests/ -k "name" -v               # one test; same two rules apply
cd frontend && npm run test:run                  # vitest
cd frontend && npm run build                     # tracked static/
./test_backend.sh · ./test_frontend.sh · ./test_all.sh
bash docker-publish.sh 0.X.Y --parallel          # EMERGENCY ONLY — a v* tag already publishes
```

- ⚠️ **Slicer sidecars:** `cd slicer-api && docker compose --profile all build && docker compose --profile all up -d` (health :3001 bambu, :3003 orca). The build context is the fork's GitHub URL, not the local checkout; the version lives in both `docker-compose.yml` and `.env`, and `.env` wins.
- ⚠️ **Backend tests run from the repository root** (the camera supervisor spawns `python -m backend.app.worker_guardian`). `conftest` sets `BAMDUDE_IGNORE_DOTENV`; do not override `DATABASE_URL`; always pass `--timeout` so a hang is a named failure. Cameras are worker-only.
- ⚠️ **`npx tsc --noEmit` checks nothing** — the root `tsconfig.json` is `"files": []` plus references. Always `npm run typecheck` (`tsc -b --noEmit`, which also checks `src/__tests__` through `tsconfig.test.json`). The pre-commit hook checks the app project only; run `npm run typecheck` before claiming green.

## Architecture

**Backend `backend/app/`:** `main.py` (FastAPI factory + lifespan + global state dicts `_active_prints`/`_expected_prints`). `api/routes/` (every router registered in `main.py`). `services/` (printer_manager, bambu_mqtt, telegram_bot, notification_service, background_dispatch, archive, virtual_printer/, obico_detection). `services/telegram_handlers/` (aiogram FSM + `auth_middleware.py`). `models/` (async SQLAlchemy) + `schemas/` (Pydantic). `core/` (config, database, auth, permissions, encryption, rate_limit, websocket). `migrations/m000_*..mNNN_*` auto-discovered. `i18n/` + `data/` (en/uk JSONs: `telegram_ui`, `notification_templates`, `maintenance_types`).

**Frontend `frontend/src/`:** React 19 + Router 7 + TanStack Query 5. `api/client.ts` is **fetch-based, NOT Axios** — single `request<T>()` helper; binary endpoints call `fetch` directly. `i18n/locales/{en,uk}.ts` keys must match. Three.js for 3D viewer. **Production bundle tracked under `static/`** — source + bundle ship same commit.

**DB:** SQLAlchemy 2.0 async (aiosqlite/asyncpg). SQLite: WAL + 15s busy + NORMAL sync. `init_db()` = `create_all()` + pending migrations. `DEBUG=true` re-runs latest migration on startup.

## Migrations

Startup: `init_db()` → legacy `bambuddy.db` rename → claim existing import-repair debt (else SQLite→PG source preflight) → `create_all()` → SQLite→PG auto-migrate (unused PG target only) → `_ensure_migrations_table` → `_bootstrap_existing` → `_run_pending` → `conform_imported_schema` (while import-repair debt is pending). The full order and its exceptions: [database-and-restore](docs/development/invariants/database-and-restore.md), `docs/development/postgres-historical-schema.md`.

- ⚠️ **The SQLite→PG move imports the file AS IS at its migration level, `_migrations` included, and the chain resumes at N+1** (`db_portable._reflect_sqlite_schema`). Never build the target from `Base.metadata` first. What SQLite never had (FK rules, NOT NULL, UNIQUE, CHECK, m001's tsvector) is added after the chain by `conform_imported_schema`, only under import-repair ownership. `info={"migration_shim": True}` columns are not canon. The import owns one prepared source through validation and load (pass it explicitly, no independent copies) and rechecks the original's fingerprint before commit. Vault: `inv-sqlite-move-imports-file-as-is`.
- Each `mNNN_*.py` has `version`, `name`, `async def upgrade(conn)` (DDL), optional `seed(session_factory)` (DML) and an optional repeat-safe `replay(conn)` for DEBUG. Helpers in `helpers.py` (`add_column`, `column_exists`, `table_exists`, `recreate_table`). Rationale lives in the docstring. Two files with one `version` fail `test_migration_versions_are_unique.py` — renumber the unreleased one.
- **Adding a column = TWO places:** the model (fresh installs via `create_all`) + a new migration via `add_column()`.
- Seeds name historical columns: INSERT through a lightweight `table`/`column` definition, never the ORM or `Model.__table__`.
- G02 B's historical PG repair uses a frozen literal whitelist, never reconciliation against live models; residuals stay named. m043/m127's PG missing-table arms are the only released-DDL exception.

## Key Invariants

The short form of every rule that must not be broken. The full text — the
reasons, the traps, what was rejected — is under the same heading in the area's
file in [`docs/development/invariants/`](docs/development/invariants/README.md);
**read it before changing code an entry names.** A test named here fails the
build when the rule is broken. `Vault: inv-x` is the maintainer's note
`40-invariants/inv-x.md`; other folders are written out.

### Platform · [full text](docs/development/invariants/platform.md)

- **Async-first.** All I/O async; pair every `asyncio.create_task` with a shutdown cancel. Never call paho's `loop_stop()` on the event loop — retire a client through `utils/paho_teardown.retire_paho_client` (outbox emptied FIRST, then DISCONNECT + `loop_stop` on a daemon thread). Guard: `test_mqtt_reconnect_replay.py`. Vault: `inv-a-replaced-mqtt-client-is-closed`.
- **The start-up heap is frozen once, first, and a running server never.** `core/process_gc.py` is the collector's one owner (`test_collector_has_one_owner.py`); `main.lifespan` calls `prepare_startup_heap()` as its first statement. Never freeze later or after a restore. `core/process_resources.py` then raises soft FD limits only. Vault: `inv-collector-startup-boundary`.
- **A request's transaction ends before its response is sent.** `get_db` is not a yield dependency; it enters `_request_session` through `enter_before_response`, and so does every new dependency that owns a transaction. A failed commit is the request's 500. No request session in a streaming body or background task. Guard: `test_get_db_commits_before_the_response.py`. Vault: `inv-request-transaction-ends-before-the-response`.
- **A null in an update body is "not sent" or a refusal — never a write into a NOT NULL column.** Inherit `schemas/not_null.IgnoresNullForRequiredColumns` (`__orm_table__`) or refuse with 422 and be listed in `test_not_null_update_schemas.py`. Vault: `inv-update-null-is-unchanged-or-refused`.
- **API refusals are translated at the boundary, never at the raise site.** Routes raise English sentences; `i18n/api_errors.py` translates them via `data/api_errors_uk.json` in the system language. What the frontend reacts to travels as `{"error": code, "message": …}` — never branch on message text; hand-built refusals use `json_error`. Guard: `scripts/api_error_catalog.py sync` + `test_api_error_details_have_ukrainian.py`. Vault: `inv-api-refusals-translated-at-the-boundary`.
- **Case folding is Unicode-aware on every backend, decided in `core/case_folding.py` — never in a route.** SQLite connections shadow `lower`/`upper`; a PostgreSQL that cannot fold compiles through a verified collation (`compiler_for`). Vault: `inv-unicode-case-folding`.
- **i18n.** No hardcoded user-facing strings: frontend `useTranslation()`, backend `t(lang, ns, "key.path")` + `escape_md()` for MarkdownV2. en + uk only — keys in BOTH.
- **Local microservices report their work through the parent log.** The parent logs readiness, loss, restart and each operation's start + terminal outcome (elapsed, correlation ID, safe summary); child output goes to a bounded buffer only; HTTP 200 or `health=ready` is no proof of work. Vault: `inv-local-microservice-parent-log`.
- **CSP + security headers.** `security_headers_middleware` is registered AFTER `auth_middleware` so it wraps 401/503; `script-src 'self'`, `frame-ancestors 'none'`, HSTS on HTTPS; `/sw-register.js` comes from `serve_sw_register()`.

### Authentication, permissions and tokens · [full text](docs/development/invariants/auth.md)

- **Auth always-on.** Every endpoint via `RequirePermission(Permission.X)`; the setup gate answers `503 setup_required` until `POST /auth/setup`. API keys (`X-API-Key` / `Bearer bd_…`) satisfy permissions; legacy keys (Bambuddy's `bb_…` included) are retired and reissued. Only `core/auth.is_api_key_token` reads the prefix (`test_api_key_prefix_is_asked_in_one_place.py`). Recovery: `python -m backend.app.cli reset_admin`.
- **Credential identity and sliding refresh.** `core/auth_lifetime.py` owns immutable account UUIDs, session generations and server-side sessions; JWTs name account + generation + session. Keys and long-lived tokens bind to an immutable owner and outlive browser sessions. Delayed writers capture scalar authority first and recheck on the publication session. Vault: `inv-auth-credential-belongs-to-immutable-account`; `docs/development/credential-lifetimes.md`.
- **2FA + OIDC.** TOTP + email OTP + 10 backup codes (`requires_2fa` + `pre_auth_token`); OIDC with PKCE S256, state, nonce, JWKS and SSRF guards; secrets Fernet-encrypted with `MFA_ENCRYPTION_KEY` (env → `DATA_DIR/.mfa_encryption_key` → generated, 0600), which the backup ZIP carries.
- **OIDC groups change only in the transaction that issues the session.** The callback writes versioned evidence (provider revision, subject, IdP groups, a fixed deadline) into the one-time token and creates a new account without groups; `services/oidc_group_sync.py` applies the managed slice under account guard → OIDC policy lock → token consumption, and every refusal is a raised 409. The provider's `policy_revision` changes with every effective policy change; a group's number is never reissued (m263). Vault: `60-specs/oidc-group-sync-n07-spec`.
- **Rate limiting.** `auth_rate_limit_events`: login 10/15 min per user, 20/15 min per IP; behind a proxy set `TRUSTED_PROXY_IPS`.
- **Permissions.** `resource:action` in `core/permissions.py`. Every new `Permission` lands in exactly one of `core/auth._APIKEY_SCOPE_BY_PERMISSION` / `_APIKEY_DENIED_PERMISSIONS` (drift-guard test) AND is seeded to Administrators in its migration.
- **A caller's printers are one scope, asked in the gate, at every token door and by every reader** (N62). `core/printer_scope` is the rule: a person = the reach of their limited groups (picked printers + locations with the ones below, D3; admin and an unlimited person reach all), a key = its own list ∩ its owner's, a chat = its list ∩ its group's reach, background work = the author's. The three gate factories run the scanner of `core/api_key_scope` (references BY NAME in path, query, JSON body — printers, queues, jobs, `archive_id(s)` — plus path resolvers) for persons (404 of the thing) and keys (403); token doors ask their holder's scope; readers narrow by `request_printer_scope` — history included (D5), rows of no printer stay. The stored lists are read only through `core/printer_scope`. Guards: `test_printer_scope_registry.py`, `test_api_key_printer_scope_coverage.py`, `test_printer_lists_are_read_by_the_scope.py`, `test_autoqueue_asks_the_author.py`. Vault: `inv-printer-scope-is-one-rule`.
- **Camera stream-token — the camera only.** Streams, snapshots and the plate-detection reference take the token from `POST /printers/camera/stream-token` (`withStreamToken()`, `useStreamTokenSync`).
- **Every other picture takes a MEDIA token and checks its own resource.** `POST /auth/media-token`; the route dependency (`require_media_permission` / `require_media_ownership_permission`) applies the resource's own rule. The two tokens are not interchangeable. Notifications link a copy from `utils/notification_photos`, never the archive's photo. Cache-busters go into the URL builder, before the token. Vault: `inv-media-token-checks-the-resource`.
- **A route called without an Authorization header reaches its own token check only through its OWN anchored pattern** in `main.PUBLIC_API_PATTERNS`, named in `tests/test_auth_public_patterns.py::PUBLIC_ROUTES`. Vault: `inv-token-in-url-routes`.
- **A retained integration credential belongs to its effective destination.** Reads show presence, never the value; writers take the installation credential anchor first and finish the transaction before network I/O; partial restore never rebinds a destination. Vault: `inv-integration-credential-belongs-to-destination`.
- **Live delivery needs current authority and owned cleanup.** Every send is guarded by a current read; the pure-ASGI media gate fails closed without a viewer; a cleanup task owns the body through completion; one viewer never stops the shared camera producer. Vault: `inv-live-delivery-requires-current-authority`.

### Database, backup and restore · [full text](docs/development/invariants/database-and-restore.md)

- **Import repair stays owned across process death.** A private `_bamdude.*` debt is written in its own transaction and cleared only by complete verification with the exact owner; unknown failures keep the web process in closed maintenance (only `/health`'s 503). No clear/force bypass. Vault: `inv-import-repair-is-durable`.
- **Portable backup carries canonical DDL and migration history.** `db_portable._export_pg_to_sqlite` builds full DDL from a metadata copy, never hand-written tables; `_migrations` travels with the rows; only completed files replace the destination. Vault: `inv-portable-backup-full-ddl`.
- **Backup file copying is strict.** `backup_files.directories()` is the shared path map; a changed or unreadable source fails the backup; links, duplicates and traversal are rejected; backup and restore share a process lock. Vault: `60-specs/backup-files-spec-plan`.
- **Portable restore preserves the previous database on import failure.** PostgreSQL DROP → load → FKs is ONE transaction; `backup_files.FileRestore` stages and verifies all files first and rolls back moves if the database fails. Vault: `inv-portable-restore-transaction`.
- **Git metadata restore cannot claim a different physical print.** Archives match by execution UUID; active or local prints take operator fields only; no accounting or completion replay. Vault: `inv-git-restore-keeps-execution-identity`.
- **A Git export needs current approval of its saved configuration.** The approving admin is rechecked before capture and before the first upload; SQL locks are released before network work. Vault: `inv-git-export-requires-current-config-approval`.
- **Content deletion has one guarded database phase and commit-owned cleanup.** `content_delete_rows` helpers only flush; the caller owns plan, commit and disk cleanup; locks follow the shared ledger order. FK actions and ORM cascades never substitute. Vault: `inv-content-deletion-owns-plan-and-commit`.
- **A library upload's initial write stays owned until the actual thread ends** (`library_file_runtime.upload_writes`); restore proves the drain or refuses. Vault: `inv-library-upload-write-is-restore-owned`.
- **Restore owns VP retirement until actual completion.** The restore latch closes every VP door; listeners close under the manager's sync lock with DB/network joins outside; delete reserves the VP ID until retirement is proven. Vault: `inv-vp-retirement-is-restore-owned`.

### Printers, MQTT and the live print · [full text](docs/development/invariants/printers.md)

- **Live layer counters belong to the observed print.** `bambu_mqtt._update_state` computes the run boundary once before reading counters; never a cached old total or an ACK. Display fills a missing total only through `layer_projection`, never into `PrinterState` or accounting. Vault: `inv-live-layers-belong-to-observed-print`.
- **Print memory and archive identity follow one observed run boundary.** An unproven file change never resets print memory; durable archive/dispatch identities decide adoption before INSERT, never downloaded hashes. Vault: `inv-print-memory-follows-observed-run`.
- **A command's acknowledgement is not the printer's status.** Request-able fields (`cfg`, `timelapse`) are read only from status frames (`bambu_mqtt.is_printer_status_frame`). Vault: `inv-command-ack-is-not-status`.
- **The crash-start pause belongs to the installation, not a printer connection.** Only new starts, automatic routing, automatic plug ON, drying and preheat consult `farm_startup_guard`; a reconnect never revokes the acknowledgement. Vault: `inv-farm-startup-admission-is-durable`.
- **A person's Stop is sent and marked in one place** — `services/print_control` (`test_print_control.py`). A door never closes the queue row of a run printing under our binding; `POST /queue/{id}/stop` with an unsent stop pauses and answers 503 `stop_not_sent`. Vault: `inv-user-stop-has-one-door`.
- **Macros.** G-code via MQTT `gcode_line` wrapped in `M1002 gcode_claim_action`; swap macros fire from `background_dispatch` and `on_print_complete`. Swap capability comes only from `core/swap_profiles.py`; a new mod is a catalog entry + two macros seeded by a NEW migration.
- **A deleted printer's runtime never passes to the next printer with its id.** `services/printer_deletion.begin` advances the record life before its first await; work holding a `Printer` proves the life before connect, bind and publish. Vault: `inv-deleted-printer-runtime-not-inherited`.
- **A printer number is never handed out again.** `printers` is `AUTOINCREMENT` on SQLite (m262); the counter starts above every number in the frozen registry `core/printer_id_bound.py`, and the SQLite→PG import reserves the same bound. Numbers above 2³¹ − 1 and a restored older copy are outside the guarantee. Guard: `test_printer_id_bound.py`. Vault: `inv-printer-number-never-reissued`.
- **Archived printers.** `archived` is independent of `is_active`; "available" = `is_active AND NOT archived` in every availability query and `printer_queues.list_queues`. Keep frontend `getPrinters()` param-free.
- **Manual jog mirrors BambuStudio, never disables endstops.** `bed_jog` sends BS's exact sequence, never `M211 S0`. The bed-slinger Z flip lives ONLY in `move_axis`; `/jog` and `/bed-jog` use opposite sign conventions; the frontend never flips a sign.
- **H2C: the rack is extruder 0, and a rack position is the operator's.** An extruder index never reaches `nozzle_mapping` untranslated; `background_dispatch._rack_dispatch` resolves docks after upload and before preheat. Vault: `inv-h2c-rack-is-extruder-0`.
- **Preheat / heat-soak stage.** Opt-in (`preheat_enabled`), `services/preheat.preheat_and_soak` between upload and `start_print`; the chamber target comes from the feeds the job's mapping names; a derived 0 skips the stage.
- **Drying while printing.** Allowlist `_DRY_WHILE_PRINTING_MIN_FIRMWARE`; never add a model without Bambu's wiki confirmation; keep the `max(40, preset − 5)` cap.
- **Scheduled drying has one writer, yields to every print and holds its own unit** (`services/scheduled_drying.py`). An empty printer state is waited for, never judged. Vault: `inv-scheduled-drying-yields-and-holds`.
- **VP nozzle authority follows the target binding**, never bridge connectivity. Vault: `inv-vp-nozzle-follows-binding`.
- **VP FTP needs the asyncio loop.** Every native launch path passes `--loop asyncio`; never drop it.
- **The next slicer pause is read from the print's own file and compared with the proven run's layer** — never `state.layer_num` or an ACK; no guess. Vault: `inv-next-pause-from-the-file-and-proven-run`.
- **Obico AI failure detection.** The verdict is Obico's own `is_failing` (`obico_smoothing.py`); never compare a raw EWM with the thresholds; each action runs once per print.
- **One AI failure detector at a time, and a restore never turns OctoEverywhere on.** `obico_enabled` / `octoeverywhere_enabled`: the settings save switches the other off, and each detector also refuses while the other flag is on. OctoEverywhere (ported from upstream) acts on its API's suggestion, never a `PrintQuality` threshold; its frames come through the camera worker by Obico's rule; a full ZIP restore turns it off with an inbox notice (`services/failure_detection_restore.py`). Vault: `inv-one-failure-detector-at-a-time`.
- **Obico evidence is an observation, not a new camera capture or print authority.** One store owns publication, quotas and deletion. Vault: `inv-obico-evidence-is-an-observation`.

### The print archive · [full text](docs/development/invariants/archive.md)

- **The archive row is created at PRINT START, before the 3MF** (`file_path=""`); `attach_3mf_to_archive` must fill what `archive_print` fills.
- **Adoption identifies the physical execution before download** and repeats no start effects. Vault: `inv-print-memory-follows-observed-run`.
- **3MF download recovery.** Empty `file_path` is the retry marker; `no_3mf_available` is set only after a failed attempt; `find_archive_for_sd_file` is the one SD-name resolver.
- **An archive's folder comes from `utils/archive_paths.archive_dir_for` — never from `file_path` by hand.** Photos via `photos_dir_for` / `find_photo`. Vault: `inv-archive-folder-from-one-helper`.
- **Archive chain-of-custody.** `source_content_hash` (non-NULL on every row) + `content_hash` + `applied_patches`; `effective_hash` is the dedup key.
- **3MF gcode patcher.** `apply_3mf_transforms` in one open/mutate/write cycle; the on-disk archive stays unpatched; `applied_patches` never infers disk state.
- **3MF copy is fsync'd** (`_copy_and_fsync`, never `shutil.copy2`).
- **Plate selection in 3MF parsing walks child metadata** (`<metadata key="index">` inside `<plate>`); always pass the plate.
- **A print door asks the content, in one function** (`sliced_source_refusal` / `archive_source_refusal`), never the extension. Vault: `inv-print-doors-ask-the-content`.
- **An archive's printer and its running status are facts, not editable fields.** Every physical print is one archive row (`test_every_print_is_archived.py`).
- **A print's wear cost has one writer and is stamped only at a measured end** (`services/wear_cost.py`); NULL, never 0. Vault: `inv-wear-cost-one-writer`.
- **«Result not checked» is a mark with one writer, never a verdict** (`services/archive_outcome.py`). Fitness is the defect count; upstream's Good/Reject is rejected. Vault: `inv-outcome-checked-is-a-mark-not-a-verdict`.

### Queue and dispatch · [full text](docs/development/invariants/queue.md)

- **Single dispatch layer.** Only `background_dispatch` runs prints: one archive per physical print, `archive_id` set in the same transaction.
- **Dispatch is claimed per PRINTER, not per item.** Every print holds a queue ROW while it runs, direct ones included; upstream's per-item `dispatching_at` is not ported.
- **The lane comes first, and the claim dispatches the row its pass planned from.** A transaction holding job rows takes all its lanes (`queue_ops.claim_lanes`), ascending, before its first row lock. Every queue writer is in `tests/queue_writers/`'s allowlist. Vault: `inv-queue-lane-first`, `inv-fk-check-is-a-lock`.
- **"Only after a successful previous print" has one reader** (`services/previous_print_gate.py`), and a success releases it. Vault: `inv-previous-print-gate-one-reader`.
- **A batch action acts in one queue** (`?queue_id=`; otherwise 409 `batch_spans_queues`).
- **Routing is NOT dispatching.** `find_eligible_printer` never gates on readiness, it only ranks (the rank: the next entry); a halted queue is a filter (`queue_halted`).
- **The printer order of a placement has one reader** (`services/printer_pick_order.py`, C01): `model_dispatch_order` and each printer's hours, last real finish and priority make one key; the router, the wake step, the rebalancer's receivers and its replay rank with it in `placement_rank` — `(ready, exact, colours, no deferred rows, order, remaining, -id)`; the replay advances the hours (printing time only) and the last finish at known print ends. Guard: `test_printer_pick_order_has_one_reader.py`. Vault: `inv-printer-pick-order-one-reader`.
- **Rebalancing moves only PENDING line work, and a move is all-or-nothing** (`services/queue_rebalance.py`, one savepoint, one commit; each printer is asked about the JOB; the replay advances the printer order's facts at known print ends). Vault: `inv-rebalance-moves-only-pending-line-work`.
- **A job that has been sent but not started is not an orphan.** Reconciliation guards on `PrintArchive.created_at` (`_JUST_DISPATCHED_SECONDS`).
- **The dispatch guard asks the plan, not the feed** (`filament_preflight.plan_holds`, only the slots the plan chose). Vault: `inv-dispatch-guard-asks-the-plan`.
- **A direct rack (H3C, P3C) is routed by Bambu Studio's candidate rule**: per hotend one bound tray plus its `bind_ams` trays, the hotend's diameter and flow the slice's; an automatic plan takes no tray `QuerySlotFeedBlock` blocks (a pin may, with a notice); two filaments may share a tray. A calibration row, exempt from routing, asks its tray's hotend at start and at dispatch. Vault: `inv-tray-number-one-home`.
- **Staggered start is capped per GROUP, and an unassigned printer is a wildcard** (`services/stagger_groups.py`). The model-import list lives only in `core/database.import_all_models`. Vault: `inv-stagger-unassigned-is-wildcard`.
- **A printer's queue is the `printer_queues` row with the printer's own id — always** (`ensure_printer_queue` is the only creator). Vault: `inv-printer-queue-id-equals-printer-id`.
- **A queued job owns its bytes, and the spool is one writer** (`services/queue_sources.py`; readers through `filament_intake.item_descriptor`; no `ref_count`). Guard: `test_every_queue_row_names_its_source.py`. Vault: `inv-queue-job-owns-its-bytes`.
- **Moving a printer job to AutoQueue transfers one existing copy** (`queue_auto_transfer`, one commit). Vault: `inv-queue-transfer-is-one-existing-copy`.
- **Failed AutoQueue print work returns only from positive live settlement.** Vault: `inv-autoqueue-failed-return-needs-live-settlement`.
- **AutoQueue notification identity is an observed fact, never display text.** Vault: `inv-autoqueue-notification-is-observation`.
- **Operator print-state recovery is addressed and never replays completion.** `services/print_state_recovery.py` alone decides what is safe; no physical commands, gate clearing or accounting replay. Vault: `inv-operator-print-recovery-is-addressed`.
- **A source's swap declaration is a captured fact with one reader and one writer.** True / False / unknown; unknown is never False; `swap_change_table` goes out at most once per execution. Guard: `test_swap_fact_has_one_reader_and_writer.py`. Vault: `inv-swap-source-is-a-captured-fact`.
- **An operator's swap declaration is bound to the bytes it was made for, and reaches only the jobs selected for it** (`services/swap_compatibility.py`; `context` has no default). Vault: `inv-operator-swap-declaration-bound-to-bytes`.
- **A slicer-connector entry is published once, and its receipt is the proof (OrcaBamDude).** Published iff `published_receipt` is set; outcomes are read only by immutable identities; a numeric id is never authority. Vault: `inv-connector-entry-published-once`.

### The Workshop: orders, products, stock · [full text](docs/development/invariants/workshop.md)

- **The Workshop's rights are four domains and one filing right, asked in one place** (`api/routes/_workshop_rights.py`). Never give Operators `archives:update_all` for filing. Vault: `inv-workshop-rights-by-domain`.
- **In the Workshop the server counts, sorts, searches and pages.** The frontend never sums or re-sorts a page. Vault: `inv-workshop-lists-and-figures-on-the-server`.
- **A Workshop entity's code is derived from its id, in one place** (`services/entity_codes.py`). Vault: `inv-workshop-codes-derived-from-id`.
- **An order's journal has one writer; its stage is a manual column** (`services/order_journal.py`). Vault: `inv-order-journal-single-writer`.
- **A product's stored facets have one writer, and it follows the plate writer** (`services/product_facets.py`). Vault: `inv-product-facets-single-writer`.
- **An order line's kit has one reader, and its configuration one writer** (`services/line_composition.py`, `services/line_config.py`). Vault: `inv-line-composition-one-reader`, `inv-line-configuration-one-writer`, `inv-part-binding-and-combination-availability`.
- **Every door that moves a product's configuration, orders or stock goes through the product gate, and locks in one order** (`services/product_gate.py`). Vault: `inv-product-gate`.
- **Finished goods are columns and a ledger with one writer** (`services/finished_stock.py`). Vault: `inv-finished-stock-single-writer`.
- **What an order line takes from stock is decided by ONE function** (`services/stock_pick.suggest`), **and lines are added in ONE transaction**. Vault: `inv-stock-pick-one-function`.
- **An order's goods are issued through ONE function** (`services/order_fulfilment.py`), **and an order completes only fully issued**. Vault: `inv-order-fulfilment-one-function`, `inv-stock-issues-single-writer`.
- **Free stock of product parts is a LEDGER with one writer** (`services/part_stock.py`). Vault: `inv-stock-ledger-single-writer`.
- **Defects have one writer, and the shelf follows them** (`services/archive_defects.record_defects`). Vault: `inv-defects-single-writer`.
- **A working order plan is consumed only by committed publication receipts.** Vault: `inv-order-plan-consumption-is-publication`.
- **A part's picture has one writer, one reader, and its name → part mapping is decided at read time** (`services/part_images.py`; frontend `components/PartThumb.tsx` only). Vault: `inv-part-image-resolved-at-read`.

### Filament, AMS, spools and slicing · [full text](docs/development/invariants/filament-and-slicing.md)

- **A tray number is computed in one place** — `utils/tray_ids.py` and its twin `frontend/src/utils/trayIds.ts`. The unit's type comes from its `info` (sticky, else its id); a direct-connected rack (H3C, P3C) from `device.holder.info2`, else `default_nozzle_count > 2`; externals are stored `(255, 0..8)`. On a direct rack the tray decides the PrintTool, no `nozzle_mapping` is sent, and routing checks the spec of the hotend a tray leads to (`utils/direct_rack.py`, Bambu Studio's candidate rule); `tray_now` 255 is PrintTool 1's input only by the toolhead's `snow`. Until N01 C it takes no load / unload, RFID, drying, K-profile or automatic calibration on a rack slot / PrintTool input (`services/direct_rack_gate.py`). Guard: `test_tray_numbers_have_one_home.py` + `trayNumbersHaveOneHome.test.ts`. Vault: `inv-tray-number-one-home`.
- **Filament attribution believes the DISPATCHED mapping, never the live one.** Priority in `usage_tracker._track_from_3mf`: stored print-cmd `ams_mapping` → queue item's → live `mapping` → colour → `tray_now`; don't reorder. Sessions belong to an archive by `archive_identity`, never by name.
- **Spoolman no-3MF tracking.** Remain% snapshot at start; rows store `filament_usage=[]`, readers use `filament_usage or []`.
- **An advertised AMS profile is not the spool.** One projection, one publisher; everything about the spool reads `services/ams_advertised_overlay.py`.
- **A slot "matches its spool" when it shows the type WE wrote, not the material column** (`tray_types_written_for`). Vault: `inv-slot-matches-what-we-wrote`.
- **A spool without a family keeps ITS type on the slot; only the profile is borrowed** (`family_for_material`, never `generic_family_for_material`). Vault: `inv-a-spool-without-a-profile-keeps-its-type`.
- **A spool sits in one slot and leaves it only on proof.** A spool number is never handed out again. Vault: `inv-a-spool-sits-in-one-slot-and-leaves-it-on-proof`.
- **A Spoolman spool printed with is archived, not deleted** (one door, `services/spoolman_delete.py`; the source guard, the activity revision and the binding fence admit it in one synchronous step). Vault: `inv-spoolman-spool-printed-with-is-archived`.
- **A Spoolman binding names its spool by number and `registered`** (m266, `services/spoolman_spool_identity.py`). Writers stamp and adopt for the whole number; whatever writes to Spoolman or the printer from a binding confirms it first; a refuted binding is not charged and goes — never during its printer's print accounting; a database change removes them all. Only Spoolman's own JSON 404 is "gone". Vault: `inv-spoolman-binding-names-its-spool`.
- **The remain% weight sync waits for the booking** (`usage_tracker.print_is_unbooked`). Vault: `inv-weight-sync-waits-for-the-booking`.
- **Refilling an inactive AMS slot is not another empty reel** (`without_stale_refill_runouts`). Vault: `inv-runout-zero-only-from-unambiguous-ecodes`.
- **The K-profile table is filed per nozzle diameter; the cache prunes only what a held table dropped.** Binders ask `utils/slot_nozzle.slot_nozzle`. Vault: `inv-kprofile-table-per-nozzle`.
- **The spool name template is display only.** No search, filter or migration leans on it. Vault: `inv-spool-name-template-is-display-only`.
- **Color names — single source of truth.** The `color_catalog` table; never reintroduce hardcoded colour tables.
- **Bulk inventory actions are selection-scoped, never filter-scoped.**
- **The slicer, the bed plate and the presets are chosen through one family, `components/slicing-setup/`** (`slicingSetupOwnership.test.ts`). Vault: `inv-one-slicing-setup-family`.
- **A preset is found by its identity, and one the slicer cannot resolve refuses the slice** (422 `preset_unresolved`, never defaults). Vault: `inv-preset-identity-and-unresolved-refusal`.

### Energy, smart plugs and sensors · [full text](docs/development/invariants/energy-and-plugs.md)

- **Every automatic plug switch goes through one seam** — `smart_plug_manager.plug_switched(..., scripts=…)`, no default.
- **Per-print energy.** The END read is forced (`force_read=True`), the start is not; route by driver attribute, never `plug_type`; never in-memory dicts.
- **The electricity price has a history with one writer, and money is worked out at the price of its own time** (`services/energy_price.py`). Vault: `inv-energy-price-history-one-writer`.
- **A stalled plug counter is a suspicion; a print's estimate is marked, never a zero and never a total.** Vault: `inv-plug-meter-stall-is-a-marked-estimate`.
- **A switched-off plug reports 0 W, as physics — not as trust in the device.** Zigbee quirks need `device_resolver=` and `cluster.get(attr)`; reports arrive on `on_event`.
- **Measurement history is written ABOVE the plug drivers, never from a device listener** (`services/measurement_history.py`, `services/measurement_buffer.py`).
- **The zigbee device-class set is closed** — plugs and sensors; a new quantity is a row in `zigbee/measurements.py`.
- **Sensor reporting has three outcomes, not two** — `unanswered` ≠ `refused` ≠ `ok`.

### Downloads, printer files and video · [full text](docs/development/invariants/media.md)

- **A download is a job that owns its transfer, its scratch and its caller's standing** (`services/media_downloads.py`; never `read_bytes`, an in-memory ZIP or a frontend Blob). Vault: `inv-download-is-an-owned-job`.
- **A file on the printer is deleted only through the file guard** (`services/printer_file_guard.py`; `test_printer_deletes_ask_the_file_guard.py`). Vault: `inv-printer-file-delete-asks-the-guard`.
- **An archive's video has one registry, one publication seam and one owner** (`services/archive_media.py`, `archive_media_runtime.py`). Vault: `inv-archive-video-one-seam-one-owner`.
- **An edit of an archive's video is a job, and every ffmpeg / ffprobe of the editor is an owned child** (`services/media_child.run`, one encoder via `services/encoder_slot.py`). Vault: `inv-video-edit-is-an-owned-job`.
- **A layer recording's frames have one owner, named in their own directory** (`services/layer_timelapse.py`; decided by manifest, never by age). Vault: `inv-layer-frames-have-one-owner`.
- **A finish photo is written where its archive is at the moment of the write, and a refused one is removed** (`services/finish_photo_store.py`); no printer command for a photo. Vault: `inv-finish-photo-written-at-the-write`.

### Part renders, previews and labels · [full text](docs/development/invariants/rendering.md)

- **A part-render attempt is over only when its process tree is proven gone.** Only the disposable worker reads the file; an unproven tree makes the runtime `ownership_uncertain` until restart. Vault: `inv-part-render-attempt-proven-over`.
- **A part render runs our verified bundle over data, nothing else.** A change to `frontend/src/part-render/*` ships a rebuilt bundle in the same commit; a render input change is a `RENDERER_VERSION` decision. Vault: `inv-part-render-runs-our-code-on-data`.
- **A platform's Node is the newest LTS line that publishes an official signed build for it; the pin script is the only writer of the pin and the golden** (`scripts/pin_render_runtime.py`). Vault: `inv-node-pin-newest-line-with-official-build`.
- **A render result is an immutable attempt directory the database points at.** Vault: `inv-part-render-attempt-dir-immutable`.
- **A single object's model area is BambuStudio's first-layer outline, found by name**, never by `identify_id`. Vault: `inv-plate-json-bbox-is-the-first-layer`.
- **A STEP thumbnail: OCP only in a disposable child, the picture published only by main.** Vault: `inv-step-cad-only-in-the-child`.
- **The STEP component's copyleft sources ship with every release** (`scripts/step_preview_sources.py`); never delete those assets. Vault: `inv-step-copyleft-sources-ship-with-releases`.
- **Server-side plate thumbnails (best-effort).** A failure returns the input unchanged; they run after `inject_source_stl_preview`.
- **Labels are data, not layout code.** A design prints at its own size or is refused, never scaled.
- **Every label draw passes one gate and runs on its own two workers** (`services/label_gate.py`).

### Library and model sources · [full text](docs/development/invariants/library-and-model-sources.md)

- **A long walk holds neither the transaction nor the loop.** The scan answers `202 {job_id}`; an empty walk against a stocked folder deletes nothing.
- **A library file's visibility has one rule, and an ownerless external file is shared** (`owner_scope_sees` / `owner_scope_clause`). **A folder has an owner and one rule too** (`services/library_folder_access.py`): `read_own` sees own, shared, holding-my-file and the way there; writes only into own, shared and the root; a by-name door uses `writable_folder_named`, never `scalar_one_or_none`. Vault: `inv-library-visibility-has-one-rule`.
- **A library file's photos have one writer and leave the disk after the commit** (`services/library_file_photos.py`). Vault: `inv-library-file-photos-single-writer`.
- **A 3D-model site is a `ModelProvider`, and the routes reach it only through one.** The Bambu Cloud credential has one seam, `services/bambu_cloud_credentials.py`. Vault: `inv-a-model-site-is-a-provider`.
- **A self-hosted model source is reached through one pinned transport** (`services/model_providers/outbound.py`; LAN and plain http allowed on purpose). Vault: `inv-self-hosted-model-source-pinned-transport`.

### Notifications and Telegram · [full text](docs/development/invariants/notifications.md)

- **Telegram dispatch authority is the chat.** Never reintroduce a provider-level event toggle for telegram.
- **A telegram provider IS a bot, and one polling session holds them all.** Never a second `Dispatcher`; a chat is the pair `(provider_id, chat_id)`. Vault: `inv-telegram-chat-belongs-to-its-bot`, `inv-one-telegram-poller-per-process`.
- **The in-app inbox is a channel, not a provider row.** Provider forms render toggles from `GET /notifications/events`, never a local list. Vault: `inv-inbox-is-a-channel-not-a-provider`.

### MCP · [full text](docs/development/invariants/mcp.md)

- **MCP authority belongs to one HTTP request.** Opt-in, key-only, a frozen `DiagnosticAccess` per request; no printer commands. Vault: `inv-mcp-authority-is-request-local` (all MCP entries).
- **MCP current-job identity is not the monitor's display choice** — conflicts leave `current_job` unknown.
- **MCP period totals apply printer scope before aggregation**; totals are never page sums, unknown is not zero.
- **MCP diagnostic history correlates IDs, never filenames**; redaction precedes search.
- **MCP HTTP setup is an explicit transport-risk opt-in, not an authority change.**

### Frontend · [full text](docs/development/invariants/frontend.md)

- **Modals close only by their buttons or Esc — never on a click outside.** Every modal renders through `components/Modal.tsx`; `modalShellOwnership.test.ts` fails any other overlay. Vault: `inv-modals-close-only-by-buttons-or-esc`.
- **`<main>` is no scroll box; a page-level `sticky` sits below the header through `--app-top`** (`stickyBelowHeader.test.ts`). Vault: `inv-main-is-no-scroll-box`.
- **The bundle parses on Safari 16.0.** No regex lookbehind or class static block; `check:baseline` ends every build. Vault: `inv-bundle-parses-on-safari-16`.

## Code Style

- **Python** Ruff (line 120, double quotes, py312); B008 ignored for FastAPI `Depends()`. See `pyproject.toml`.
- **TS** ESLint flat config, strict mode.
- **Commits** Conventional (`feat`, `fix`, `docs`, `test`, `refactor`, `chore`).
- **Branches** `feature/`, `fix/`, `docs/`, `refactor/`, `test/`.
- **PRs** target `dev`, not `main`.
- **CHANGELOG** user-facing only — no `temp/*` or audit MD refs.

## Branching & Release

- `main` — production (`kainpl/bamdude:latest`, `ghcr.io/kainpl/bamdude:latest`). `dev` — active development; CI on push + PR; a push to `dev` publishes no image. `feature/*` — short-lived; long upstream-port branches merge `dev` periodically.
- **Stable** `vX.Y.Z` on main → `:latest` + `:X.Y.Z`, `gh release create` after the dev→main FF. **Docker publishes itself:** a `v*` tag fires `docker-publish-tag.yml` (amd64 + arm64 built natively, pushed by digest, tagged after the sources check), `windows-installer.yml` and `publish-code-graph.yml`; never also run the script.
- **Beta** `vX.Y.ZbN` on dev → `:X.Y.ZbN` + `:dev` (no `:latest`). `docker-publish-dev.yml` is a manual dispatch for the raw `dev` head only.
- **The CI gate is the run on `dev`, never the one on `main`:** a FF to `main` is the same SHA, so `check-duplicate` skips every job there and reports ✓ having tested nothing. Order: push `dev` → `gh run watch --exit-status` → FF `main` → tag.
- **Naming:** tag `v<version>`; release title `BamDude v<version>` plus ` (pre-release)` for prereleases, no subtitles. Drift check (must print nothing): `gh release list --limit 100 --json tagName,name,isPrerelease --jq '.[] | select(.name != ("BamDude " + .tagName + (if .isPrerelease then " (pre-release)" else "" end)))'`. Fix a title with `gh release edit <tag> --title`, never by re-releasing.
- **Tags are immutable** — never force-push; ship `X.Y.ZbN+1` or `X.Y.Z.1`.

## Adding Features

- **API endpoint** — schema in `schemas/` + route in `api/routes/` + register in `main.py` + permission if needed.
- **DB model** — model file + `database.py` import + migration + schema + route.
- **Column** — model file (fresh installs) + `add_column()` in a new migration (existing DBs) + Pydantic schemas + frontend types.
- **Telegram handler** — handler file + register in `telegram_bot.py` + i18n in BOTH `telegram_ui_{en,uk}.json`.
- **Notification trigger** — `notification_service.py` + BOTH `notification_templates_{en,uk}.json`.
- **Maintenance type** — `DEFAULT_MAINTENANCE_TYPES` in `routes/maintenance.py` with `type_code` + BOTH `maintenance_types_{en,uk}.json`; model-specific → `_ROD_TYPE_REQUIREMENTS`.
- **Public frontend feature** — `npm run build`; `static/` ships in the same commit (GitHub builds do not replace the committed bundle).
- **A new load-bearing rule** — a short entry under its area in *Key Invariants* + its full text under the same heading in `docs/development/invariants/<area>.md` (a new area is a new file + a row in that folder's README). A changed rule changes both.
- **Anything a user can see or an operator relies on** — the documentation site (separate repo `docs.bamdude.top`: every page describing the behaviour, English and its `.uk.md` twin, `docs/reference/settings.md` for settings) and, for a new feature or a changed scope, the landing (`bamdude.top`), in the same batch. Correct what the change made false; labels come from the locale files, claims from the code; no "since version X" notes. A change that needs no documentation says so in its report.

## Upstream (Bambuddy)

BamDude is a hard fork of Bambuddy. Upstream releases are **adapted, not
merged**: a per-release audit marks every upstream change applied or rejected
with a reason. Before porting, audit our own state around it — the two-tier
queue (per-printer + AutoQueue distributor), the per-printer dispatch claim,
the archive chain-of-custody — and adapt the change onto that, never
re-implement upstream's flat model beside it.

## Bambu Studio reference

Bambu Studio is the protocol authority for Printer Settings, AMS Settings,
Filament Calibration, the `print.command="*"` dispatch mappings, gcode stage
strings and the calibration assets in `backend/app/data/calib_assets/`. We
mirror **protocol and assets only** — BS code is never merged.

### Mirrored printer configs (`backend/app/data/printers/`)

Copies (parsed-JSON-equivalent) of the complete BS `resources/printers/*.json` catalog plus `filaments_blacklist.json`: the source of truth for model descriptions, compatibility and **per-model device capabilities** (`support_*_calibration`, `support_bed_leveling`, chamber, camera, …). Read by `utils/printer_configs.py` (`load_printer_config`, `device_calibration_availability`, `resolve_device_calibrations`, which overrides the JSON with the printer's live `support_*` MQTT flags) and `utils/model_compatibility.py`. Re-sync from the chosen BS Git ref, comparing complete file sets and parsed JSON — protocol and source tag in that folder's `README.md`. Files are named by internal code (`N6.json` = X2D); the loader keys off each JSON's own `display_name`/`model_id`, not `PRINTER_MODEL_ID_MAP` (`C11` = P1P, `C12` = P1S, `BL-P001` = X1C, `BL-P002` = X1). New models are recognised by backend and frontend name maps without guessing capabilities. Device calibrations: `{"print":{"command":"calibration","option":<bitmask>}}` — bits 0 lidar (xcam), 1 bed_leveling, 2 vibration, 3 motor_noise, 4 nozzle_offset, 5 high_temp_bed, 6 clump_pos.
