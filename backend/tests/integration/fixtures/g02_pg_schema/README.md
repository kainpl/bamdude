# G02 historical PostgreSQL fixtures

These SQLite files were created by the actual historical producer commits in
`provenance.json`, with synthetic data only. They are not current metadata
with migration markers removed. The gzip members have deterministic timestamps;
the manifest verifies both compressed and expanded bytes before import.
Fixture `v040` is the unsupported-source control. The supported set starts at
`v042`. Each receipt distinguishes the peeled producer commit from an annotated
tag object and records the requirements blob hash and producer image identity.
Resolved dependencies satisfy historical lower bounds using modern versions.

`retry*.sql.gz` came from actual failed PostgreSQL imports, with the applied
marker immediately before their respective failures. `retry-provenance.json`
pins their expanded SQL and marker level. A trusted-fixture loader uses PG's
COPY protocol, resets restore-only settings, and never invents old schemas.

`sqlite-ddl.json` pins rendered released SQLite CREATE statements from the
accepted base. `backend/app/data/pg_schema_b_retained_drift_v1.json` contains
the accepted exact old/new
structural pairs. `null-residuals.json` names the accepted columns where unknown
NULL values are retained; the oracle additionally requires a positive count.
No wildcard exclusions or SQL-default execution are used. Runtime and the gate
read that same versioned file and `core/pg_schema_contract.py` normalisation.
