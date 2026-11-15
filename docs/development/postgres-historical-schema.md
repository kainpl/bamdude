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

## Unreleased gates still open in G02 D4

The current development tree is not release-ready. Fresh head-SQLite sources
can contain a named unique index duplicating an auth constraint, which the PG
import rejects. Portable backups also omit other migration-only indexes,
including a partial unique calibration index. B restores only the frozen B
targets; it does not resolve that complete index inventory. D4 must cover
fresh and already-affected SQLite sources and both portable restore dialects
before a release. The historical fixture upgraded to head is a separate test;
its success does not close these two gates.
