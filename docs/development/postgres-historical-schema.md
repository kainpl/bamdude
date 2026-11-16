# Historical PostgreSQL upgrades and SQLite imports

SQLite-file imports into PostgreSQL require migration level m041 (BamDude
v0.4.2) or newer. Missing, malformed or older migration history is refused
before destination schema creation/replacement. Upgrade a **copy** using a
SQLite installation, then make a new backup. Ordinary SQLite upgrades and
restores into SQLite retain their existing compatibility. A populated
PostgreSQL installation ignores a stray old local SQLite file.

The level probe opens a temporary copy of the DB and any existing WAL. It
does not create WAL/SHM sidecars beside the source. A source changing during
that copy is refused; stop its writer before trying again.

Identity, migration-level checks, effective-type preflight and the actual
import share one owned private DB/WAL snapshot. PostgreSQL widths come from
the same reflection that builds its target schema: current model types
override known columns, while unknown legacy VARCHAR widths still apply.
Overlength values, including excess spaces PostgreSQL could truncate, and
NUL in text/JSON are refused with field counts before restore drains. JSON's
escaped NUL is refused even for PG json, which is more permissive than jsonb.
The original's fingerprint is checked again before commit and consumption.

The unreleased auth-lifetime migration skips an extra notification dedupe
index when a full UNIQUE guarantee already exists. Importing an already
affected source omits only the proven identical index from its translated
schema; source bytes and the UNIQUE constraint remain intact. A different
predicate, expression, key, ordering or collation fails preflight.

The historical PG repair is a numbered, additive migration with frozen
targets. It does not compare the running database with current ORM models on
every start. It repairs the observed REAL precision, missing write defaults,
nullable flags, references, uniqueness, indexes and PostgreSQL search objects.
Released migration edits are limited to the missing-table PostgreSQL arms of
m043 and m127; their rendered SQLite DDL is unchanged. The shared column
helper translates DATETIME/BLOB types without editing released callers.

Import conformance replays that frozen PG repair even when the source already
carries its migration marker. On SQLite the numbered migration is a no-op;
its marker alone does not prove that PostgreSQL objects exist. This replay is
import-only; ordinary native PG boots still use the numbered migration chain.

Unknown timestamps, duplicate identities and native-upgrade orphans that
cannot be safely detached are preserved and reported by object name and
count. Historical NULL timestamps are never replaced with the upgrade time.
The import-only conformance pass retains its existing, separate orphan
policy; a diagnostic emitted before that pass is not a final retention claim.

Archive removal detaches `spool_usage_history.archive_id` on PostgreSQL. The
usage row, grams, cost and outcome remain. The ordinary archive writer also
detaches explicitly on SQLite; bulk SQLite deletion doors still need G02 C's
separate deletion policy. This PG repair does not enable SQLite FK enforcement.

Search repair checks catalog definitions and validation/enabled/ready flags.
A disabled trigger requires rebuilding stale non-NULL vectors too. Correct
components are not recreated. A backfill failure stops startup and rolls back
the migration without its marker. In particular, oversized search vectors can
exceed PostgreSQL's size limit; repair the source content with administrator
assistance rather than expecting BamDude to truncate notes silently.

Plan maintenance time for a historical repair on a large database. A NULL
count and a successful `SET NOT NULL` each scan the affected column; PostgreSQL
holds an ACCESS EXCLUSIVE lock for the ALTER until transaction commit. Other
writers can wait. The addressed acceptance bundle records both scans, lock
wait, observed lock hold and a concurrent writer on one million history rows.

`DEBUG` can use a migration's explicit repeat-safe `replay()` hook instead of
deleting an already-applied marker. This repair's replay checks metadata but
does not scan correct FTS data again. Ordinary production restarts do not run
an applied repair.

The existing PostgreSQL-scenarios CI job owns the regression gate:

```sh
TEST_POSTGRES_URL=postgresql://... python -m pytest \
  backend/tests/integration/test_postgres_scenarios.py -k g02_historical \
  --timeout=120 -n 0
```

It destroys the scratch database's **public schema**, including functions and
unowned sequences. Use a disposable database. Synthetic producer fixtures,
their code/requirements hashes and the exact drift rules live in
`backend/tests/integration/fixtures/g02_pg_schema/`. Dependency requirements
are historical bytes; their resolved environments are modern resolutions of
lower bounds, not historical deployment lockfiles.

## Durable import repair and recovery

An imported migration marker does not prove the destination's schema. Every
database replacement writes a new private repair debt in the replacement
transaction (in the prepared image on SQLite). PostgreSQL resumes the whole
post-import conform: historical repair, portable indexes, columns and NOT NULL,
UNIQUE, foreign keys, model indexes and CHECK. Only final verification and
removing that exact debt owner share the finishing transaction. A process
crash between phases leaves durable work for the next start.

The comparator uses the installed model plan, frozen historical policy and
the exact versioned accepted B pairs in application data. It does not execute
defaults from a source file or grant wildcard exceptions. Extra objects outside
the application plan are not silently dropped.

Known violations of data-dependent guards retain rows and a count-only,
nonblocking residual when that exact guard is actually missing. This does not
replace the existing import FK policy: dangling SET NULL references are still
detached, and other orphan rows are still purged before classification. Missing
guards are retried individually after their data is corrected, rather than
reconciling every running installation against current models on each boot.
Portable export re-proves its omissions in the same database snapshot that
copies rows; an old report is not permission to omit a now-enforceable guard.

Duplicate active filament calibrations need a separate explicit confirmation
in Settings → Backup, before services stop. Restore retains the source rows
and active flags, without the partial uniqueness protection, and reports the
warning. The short-lived, one-use confirmation is bound to the administrator,
the entire ZIP and prepared database, destination and exact count. Its audit
and new debt are committed with replacement. Approval in a source backup is
never accepted. Automatic SQLite-to-PG import cannot supply this consent:
make a consistent ZIP from a copy of the source, start a clean installation
and use the explicit restore flow.

The complete portable index set is declared in models and repaired by a
frozen additive migration. Native upgrades with duplicate active calibrations
remain usable with a durable warning. The intentionally redundant legacy
token-identity index remains in this inventory. Unknown same-name indexes are
never dropped based on a guessed equivalent expression.

If a technical or unknown-structure failure leaves hard debt, startup stays in
maintenance: `/health` and other requests return safe 503, and farm, auth and
WebSocket admission remain closed. Two automatic attempts are recorded before
work starts; kills do not refund them. A known failure, including a temporary
lock or connection failure when it can be recorded, exhausts the automatic
budget. An unhealthy-container restart policy may restart the process, but
cannot refill this budget. Correct the external cause and run offline recovery:

```sh
python -m backend.app.cli schema_repair status
python -m backend.app.cli schema_repair retry
```

Stop BamDude first; commands refuse its held installation lock. `status` is
read-only and does not initialise a missing database, embedded password/port,
migrations or farm. `retry` makes one explicit attempt, including when the
automatic budget is exhausted, and never supplies consent or clears debt by
force. A successful repair still requires normal application restart.

Complete a pending hard debt before a schema/model-changing application upgrade.
Its whole-import manifest intentionally belongs to that code generation: an image
with a different manifest refuses it as `unknown_manifest`, and retry cannot
adopt or clear it by force. Maintenance fixes that retain the same manifest can
still repair it. If the application was already upgraded before any new repair work,
return to the previously pinned application version with the same data and
connection, complete repair there, then upgrade normally. Never edit the debt
marker or blindly roll back the database schema to bypass this check.

Nonblocking R5 records have a different lifetime. Retry re-derives each object's
server-normalised definition, so unrelated model changes do not strand its guard.
A changed object definition remains recorded with a warning. An optional retry
error rolls back its work and retains the records without stopping native startup
or creating hard debt; the explicit CLI returns a failure for that incomplete
attempt. Migration/repair failures log only their stage/version and exception
class, never row values, paths or raw driver error details.

For Docker, stop the application service (it is PID 1 in its container), then
run the command in a one-shot container of the same image with the same data
mount, environment and UID. Keep an external PostgreSQL service running.
For a Windows service, stop it and confirm its process has ended; invoke its
bundled Python from the application directory with the same DATA_DIR and
DATABASE_URL. A separately registered PostgreSQL service may remain running. For an existing embedded-child cluster, retry starts only the required database child and proves its cleanup; it never initializes a replacement cluster.

To prepare a raw SQLite source for explicit restore, use SQLite's backup API
against an isolated copy of its DB/WAL pair to produce a consistent
`bamdude.db`. Put it at the ZIP root with the source's required queue, library,
keys and companion files under the existing backup validation rules. Do not
edit or rename the original to bypass preflight.

D4's implementation acceptance and G02 C remain separate release gates. A
historical schema test alone does not close either gate.
