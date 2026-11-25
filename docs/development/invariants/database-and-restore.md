# Invariants: Database, backup and restore

Full text of the rules that [`CLAUDE.md`](../../../CLAUDE.md) keeps in short form, with the reasons and the history behind them. `Vault:` pointers name notes in the maintainer's private knowledge base; everything a contributor needs is here.

## Startup order and the SQLite → PostgreSQL import

The Migrations section of `CLAUDE.md`, in full.

Startup: `init_db()` → legacy `bambuddy.db` rename → claim existing durable import debt before DDL; when absent, eligible SQLite→PG source-level preflight → `Base.metadata.create_all()` → SQLite→PG auto-migrate (unused PG target only). Existing PG debt skips create_all/local re-import so pending AS-IS converters retain their legacy input; SQLite keeps its historical create_all even under debt (legacy migrations need create_all-only tables), after spending its attempt → `_ensure_migrations_table` → `_bootstrap_existing` (marks m000+m001 applied for legacy) → `_run_pending` → `conform_imported_schema` (while durable import-repair debt is pending).

⚠️ **The SQLite→PG move imports the file AS IS at ITS migration level, `_migrations` included, and the chain resumes at N+1** (`db_portable._reflect_sqlite_schema`; 2026-09-09). Never build the target schema from `Base.metadata` before the load — the models lack exactly the legacy columns/tables the later converters read (m157 `printer_id`, m158 project pivots), and re-running the whole chain on PG is how a dialect bug in an old migration reached a tester while the maintainer's own SQLite→PG path never executed it. What no migration ever did on SQLite (FK rules, NOT NULL, UNIQUE, CHECK, m001's tsvector half) is added after the chain under durable import-repair ownership by `conform_imported_schema` — never by a generic pass on an ordinary native boot. A process flag or an imported migration marker is not proof of completion. Model columns marked `info={"migration_shim": True}` (notification.py) are not canon and are skipped there. Vault: `40-invariants/inv-sqlite-move-imports-file-as-is.md`.

Each `mNNN_*.py` has `version`, `name`, `async def upgrade(conn)` (DDL), optional `async def seed(session_factory)` (DML). Helpers in `helpers.py`: `add_column`, `column_exists`, `table_exists`, `recreate_table`. Per-migration rationale lives in migration docstring — read there, don't duplicate here. Two branches can each take the same next number; `tests/unit/test_migration_versions_are_unique.py` fails when two files share a `version` or a file's number is not its `version` — renumber the unreleased one.

**Adding a column = TWO places:** model file (fresh installs via `create_all`) + new migration via `add_column()` (existing DBs).

Migration seeds name historical columns for reads and writes. For INSERT use a lightweight SQLAlchemy `table`/`column` definition; ORM inserts and `Model.__table__` both inherit future Python defaults and can reference columns absent at that migration step.

G02 B's historical PG repair uses a frozen literal whitelist, never ordinary-startup reconciliation against live models. Unknown NULLs, duplicate identities and unsafe native orphan repairs remain named/count residuals. Its guarded FTS helper is shared by import conformance; a failed backfill rolls back and stops startup. A migration may provide an explicit repeat-safe `replay(conn)` for DEBUG without deleting its completed marker. The narrow released-DDL exception changes only missing-table PG arms in m043/m127; SQLite rendered DDL stays byte-identical. SQLite-file→PG has a preflight floor; ordinary SQLite/native-PG upgrades do not. See `docs/development/postgres-historical-schema.md`.

PostgreSQL import owns one prepared DB/WAL source through identity/floor,
effective-type validation and load. Pass that owner explicitly; validators
must not make independent copies or open the original with SQLite. The
reflected PG types own width/NUL validation, including unknown legacy columns.
Before commit/consumption recheck the original fingerprint. Notification
UNIQUE name-collision translation is exact and proof-based, never a generic
index-name dedupe.

## Import repair stays owned across process death

Replacement writes a new
private `_bamdude.*` debt in its own transaction (inside the SQLite image).
Spend the durable automatic budget before any repair DDL, preserve debt through
all migrations/conform phases, and combine complete verification with an exact
owner clear. Known data failures require both a canonical violation proof and
an actually missing guard; technical/unknown structure failures keep the web
process in closed maintenance. Only `/health`'s safe 503 shell remains; ASGI
startup.complete must not open farm/auth admission there. Ordinary native boots
retry only persisted exact residuals, bound to their own server-normalised
object definition rather than the whole model hash. Optional retry errors
retain the record and diagnostic without inventing a hard startup debt.
Public settings reset, read, Git export
and partial restore do not own this namespace. Offline status never initializes
configuration, migrations or farm, and retry has no clear/force bypass.
Vault: `40-invariants/inv-import-repair-is-durable.md`.

## Portable backup carries canonical DDL and migration history

`db_portable._export_pg_to_sqlite` creates a separate SQLite metadata copy, omitting `migration_shim` bootstrap columns without mutating global models. Never replace full DDL with hand-written name/type tables. `_migrations` must travel with the rows; SQLite FTS is rebuilt without replaying baseline migrations. PostgreSQL export reads one read-only snapshot and refuses unhandled source schema; SQLite export uses the online backup API, including WAL. Only completed files replace the destination. NULL coalescing uses Python defaults or datetime server-defaults, never guessed boolean/integer SQL defaults. Vault: `40-invariants/inv-portable-backup-full-ddl.md`.

## Backup file copying is strict

`backup_files.directories()` is the shared path map (including configured archive/calibration roots); empty directories are included. Source changes, unreadable files and an existing corrupt Zigbee DB fail the backup. New ZIPs carry a size/SHA-256/directory manifest; old ZIPs still get path/CRC/DB validation. Reject links/junctions, duplicate paths and traversal. Filesystem workers must be awaited even on cancellation; backup/restore share a process lock. Stop Zigbee cleanly before replacing its database/sidecars. Vault: `60-specs/backup-files-spec-plan.md`.

## Portable restore preserves the previous database on import failure

Validate the SQLite file before restore side effects. PostgreSQL DROP, CREATE, load, sequences and FK restoration share ONE transaction; FK repair uses savepoints within it, and conflicting rows must not be silently skipped. The source file still owns its schema/migration level. Release the request's session before DROP. `backup_files.FileRestore` stages and verifies ALL incoming files on their destination filesystems, retains old contents without replacing mount roots, and rolls back file moves if replacement of the DB fails. Commit file state immediately after DB success (inside the SQLite worker, to survive cancellation); later migrations must see matching new DB and files. A failed rollback retains recovery copies, never deletes them. The whole ZIP is not a power-loss-safe distributed transaction. Vault: `40-invariants/inv-portable-restore-transaction.md`.

## Git metadata restore cannot claim a different physical print

Archives match
their execution UUID; legacy keys must be usable and unambiguous, and never
become a fallback match for a different UUID. Active or uncertain snapshots
and local prints allow only operator fields. File paths, order links and
existing execution identity stay local. Energy numbers and provenance form one
bundle. Usage links recheck the committed archive UUID, not just its integer id,
and retain spool/printer row lifetimes through their write. Categories commit
separately; metadata import never replays physical accounting or completion.
Until restore admission/drain is accepted, the farm must be idle.
Vault: 40-invariants/inv-git-restore-keeps-execution-identity.md.

## A Git export needs current approval of its saved configuration

The approving immutable account must remain active, admin and allowed to back up; session/password resets do not revoke that independent approval. Legacy configurations require an explicit admin save. Run admission claims a due slot and advances only its own cadence under the existing credential anchor, then releases SQL locks before network work. The exact configuration and current approver are checked again before capture and before the first snapshot upload. A blocked episode, terminal attempt log, notification marker and eligible admin inbox rows commit together through the existing staged inbox seam; postcommit delivery cannot rewrite a terminal result. Log completion uses the attempt identity, never a reused numeric id. Partial restore validates ordinary editor fields and preserves local destinations; it guards the original browser authority after remote reads and before each publication. Full restore drains the owning Git work before replacing storage. Vault: `40-invariants/inv-git-export-requires-current-config-approval.md`.

## Content deletion has one guarded database phase and commit-owned cleanup

Archive/library helpers in `content_delete_rows` only flush; callers own the
whole plan, commit and exact disk cleanup. Library deletion plans all archive
and lane process locks before SQLite's writer, then library rows, clean product
gates, orders/positions/lines, prints and jobs in the shared ledger order.
Source refresh uses that same library-before-job order. Completion commits its
terminal fact before a fresh library-only counter transaction, which rereads
the archive link after the library lock wait and takes no print/job lock.
Transient dispatch captures library identity at admission; cleanup cannot gain
authority by looking up a late numeric id. Assembly cleanup uses one fresh
owning transaction per file. User-content deletion bounds all affected parents
and children before its writer and refuses physical keep/C1 conditions before
any removal. SQLite FK actions and ORM parent cascades are never substitutes
for these policies. Vault: `40-invariants/inv-content-deletion-owns-plan-and-commit.md`.

## A library upload's initial write stays owned until the actual thread ends

`library_file_runtime.upload_writes` registers it before yielding; repeated
request cancellation cannot detach it. Restore closes admission and proves
that these writes have drained before replacing storage, or refuses restore.
Shutdown instead logs an unproven drain and continues stopping services; write
admission stays closed and actual threads remain owned until interpreter exit.
Only lifespan startup reopens admission. This owner adds no database or queue
spool lock and covers the initial raw write, not the whole ingest transaction.
Vault: `40-invariants/inv-library-upload-write-is-restore-owned.md`.

## Restore owns VP retirement until actual completion

The canonical restore
restart latch closes every ordinary VP runtime door; generic crash/operator
holds do not. FTP and MQTT lease local business before acknowledgement, and a
timeout/cancel leaves one reachable owner running. Never cancel that business
between library and queue commits. Only close listeners and publish ownership
under the manager's non-reentrant sync lock; DB/network/task joins stay outside.
Close idle writers (short TLS grace, then abort) before joining services.
Transparent proxies are cut/joined and reported as interruptions, not completed
uploads. A replacement waits for predecessor proof and the latest configuration.
Git goes from VP proof to its categories; ZIP retains its other quiesce/fence
contracts. Both use the existing operation guard; scheduled local busy is a
retry, not failure. VP drain does not make physical-print writers quiet.
ZIP closes G01 admission synchronously before yielding, proves VP completion,
then joins those owners. Keep the later upload-write/connector barriers and
restore-actor validation under the storage guard before file replacement.
Exited business errors and completed failed syncs are not pending I/O. Finish
independent idle teardown before reporting failure; retain an explicit unproven
owner rather than a failed retirement task that blocks unrelated VPs. Pending
roots permit retry, completed unproven roots require restart. Delete reserves
the VP ID through commit and retirement proof, cleaning its uploads before reuse.
Every retirement outcome wakes deletion-reservation waiters; failure keeps the
ID reserved. Mark the saved VP pending before joining a busy sync coordinator,
including target detaches committed by physical-printer deletion.
Vault: `40-invariants/inv-vp-retirement-is-restore-owned.md`.
