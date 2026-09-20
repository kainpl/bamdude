"""Response shape for ``GET /system/database``.

Every field is optional on purpose: a probe that fails leaves its own field
``None`` and names itself in ``probes_failed``, because this is the page an
operator opens when the database is already misbehaving.
"""

from pydantic import BaseModel


class PoolStatus(BaseModel):
    dialect: str | None = None
    config: dict | None = None
    current_size: int | None = None
    checked_out: int | None = None
    checked_in: int | None = None
    overflow: int | None = None


class SlowStatementOut(BaseModel):
    statement: str
    count: int
    total_ms: float
    mean_ms: float


class Instrumentation(BaseModel):
    query_threshold_ms: int
    request_threshold_ms: int
    #: ``pg_stat_statements`` or ``in_process`` — SQLite has no such view, which
    #: is why BamDude keeps a bounded table of its own.
    source: str
    #: Why the list is empty, when it is. An empty list with no reason would
    #: read as «no slow queries» rather than «nobody was counting».
    reason: str | None = None
    slowest: list[SlowStatementOut] = []


class SqliteHealth(BaseModel):
    journal_mode: str | None = None
    page_size: int | None = None
    page_count: int | None = None
    freelist_count: int | None = None
    busy_timeout_ms: int | None = None
    cache_size: int | None = None
    wal_bytes: int | None = None
    shm_bytes: int | None = None


class PgConnections(BaseModel):
    used: int
    max: int


class PostgresHealth(BaseModel):
    connections: PgConnections
    cache_hit_ratio: float | None = None
    commits: int = 0
    rollbacks: int = 0
    deadlocks: int = 0
    temp_files: int = 0
    temp_bytes: int = 0


class TableSize(BaseModel):
    table: str
    bytes: int
    rows: int


class TableScans(BaseModel):
    table: str
    seq_scan: int
    idx_scan: int


class DbHealth(BaseModel):
    engine: str
    version: str | None = None
    #: ``sqlite`` / ``embedded`` / ``embedded_service`` / ``external`` — NOT the
    #: dialect: ``DATABASE_URL=embedded`` is a PostgreSQL URL by the time the
    #: engine sees it.
    mode: str
    size_bytes: int | None = None
    pool: PoolStatus | None = None
    instrumentation: Instrumentation
    sqlite: SqliteHealth | None = None
    postgres: PostgresHealth | None = None
    largest_tables: list[TableSize] | None = None
    scans: list[TableScans] | None = None
    probes_failed: list[str] = []
