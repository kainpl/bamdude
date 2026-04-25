---
title: PostgreSQL Support
description: Optional PostgreSQL database backend for large print farms
---

# PostgreSQL Support

BamDude supports an optional PostgreSQL database backend for users who need better concurrency, replication, or integration with existing infrastructure. SQLite remains the default — no configuration needed.

---

## :material-database: When to Use PostgreSQL

| Scenario | Recommended |
|----------|:-----------:|
| Single user, 1-5 printers | SQLite |
| Small farm, < 10 printers | SQLite |
| Large farm, 10+ printers | PostgreSQL |
| High concurrency (many API clients) | PostgreSQL |
| Need database replication/backup | PostgreSQL |
| Existing PostgreSQL infrastructure | PostgreSQL |
| Simple setup, no extra services | SQLite |

---

## :material-cog: Configuration

### Environment Variable

Set `DATABASE_URL` to switch from SQLite to PostgreSQL:

```bash
DATABASE_URL=postgresql+asyncpg://bamdude:password@localhost:5432/bamdude
```

| Component | Value |
|-----------|-------|
| Driver | `postgresql+asyncpg` (required) |
| User | Database user |
| Password | Database password |
| Host | PostgreSQL server address |
| Port | Default `5432` |
| Database | Must already exist |

### Docker Compose

```yaml
services:
  bamdude:
    image: ghcr.io/kainpl/bamdude:latest
    network_mode: host
    environment:
      - TZ=Europe/Kyiv
      - DATABASE_URL=postgresql+asyncpg://bamdude:password@localhost:5432/bamdude
    volumes:
      - bamdude_data:/app/data
      - bamdude_logs:/app/logs
    restart: unless-stopped

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: bamdude
      POSTGRES_USER: bamdude
      POSTGRES_PASSWORD: password
    volumes:
      - postgres_data:/var/lib/postgresql/data
    ports:
      - "5432:5432"
    restart: unless-stopped

volumes:
  bamdude_data:
  bamdude_logs:
  postgres_data:
```

### .env File

```env
DATABASE_URL=postgresql+asyncpg://bamdude:password@postgres:5432/bamdude
```

---

## :material-swap-horizontal: Migrating from SQLite to PostgreSQL

### Automatic Migration

When you switch to PostgreSQL for the first time:

1. Set `DATABASE_URL` in your environment
2. Restart BamDude
3. BamDude detects:
    - PostgreSQL is empty (fresh database)
    - Local `bamdude.db` exists
4. **Automatically transfers all data** from SQLite to PostgreSQL
5. Renames `bamdude.db` → `bamdude.db.migrated`

!!! info "No manual steps required"
    The migration is fully automatic. All tables, settings, archives, spools, queues, and user accounts are transferred.

### What Gets Migrated

- All tables and data (printers, archives, spools, settings, users, etc.)
- Type conversion: SQLite boolean (0/1) → PostgreSQL boolean, datetime strings → timestamps
- Sequences reset to correct values (auto-increment IDs)
- Full-text search index rebuilt using PostgreSQL tsvector + GIN

### What Does NOT Migrate

- FTS5 virtual tables (replaced by PostgreSQL tsvector)
- WAL/SHM files (SQLite-specific)
- The `_migrations` table (recreated fresh)

---

## :material-backup-restore: Backup & Restore

### Portable Backup Format

Backups are **always in SQLite format** regardless of database backend. This ensures:

- Backups from PostgreSQL can be restored on SQLite (and vice versa)
- Backups are single-file, portable, and easy to inspect
- No dependency on `pg_dump` or other tools

### Backup (PostgreSQL → SQLite ZIP)

1. Go to **Settings** > **Backup**
2. Click **Create Backup**
3. BamDude exports all PostgreSQL tables to a temporary SQLite file
4. Packages it with archives, icons, and other data directories into a ZIP

### Restore (SQLite ZIP → PostgreSQL)

1. Go to **Settings** > **Backup**
2. Upload a backup ZIP
3. BamDude imports the SQLite data into PostgreSQL with automatic type conversion

---

## :material-magnify: Full-Text Search

BamDude uses different full-text search implementations depending on the database:

| Feature | SQLite | PostgreSQL |
|---------|--------|------------|
| Engine | FTS5 virtual table | tsvector + GIN index |
| Query syntax | `MATCH` with wildcards | `to_tsquery` with prefix matching |
| Triggers | INSERT/UPDATE/DELETE triggers | BEFORE INSERT OR UPDATE function |
| Rebuild | Delete + re-insert FTS rows | UPDATE trigger re-fires |
| Weights | Not weighted | A (name) > B (filename, tags) > C (designer, filament) > D (notes) |

Both are transparent to the user — the search API works the same way.

---

## :material-connection: Connection Pool

| Setting | SQLite | PostgreSQL |
|---------|--------|------------|
| Pool size | 20 | 10 |
| Max overflow | 200 | 20 |
| Busy timeout | 15s (PRAGMA) | Connection-level |

---

## :material-clock-alert: Slow first-boot migrations

Some migrations are intrinsically slow regardless of database backend because the bottleneck is opening 3MFs on disk, not DB writes:

- **m022** (0.4.1) reads one config file from inside every existing 3MF to backfill the new `gcode_label_objects` + `exclude_object` flags. Roughly 50-200 ms per file. An install with thousands of archives can spend several minutes inside the migration step before the API comes up. Same wall-clock cost on PostgreSQL as on SQLite.

The migration logs progress every 100 rows -- watch for `m022 library_files: progress` and `m022 print_archives: progress` lines if you think the boot has hung. Files that have been deleted from disk are skipped silently.

---

## :material-alert: Limitations

!!! warning "Create the database first"
    BamDude does **not** create the PostgreSQL database — it must already exist. Only tables are created automatically.

!!! warning "No pg_dump via UI"
    The web UI backup always exports to SQLite format. For native PostgreSQL backups, use `pg_dump` directly.

!!! tip "Reverting to SQLite"
    To switch back to SQLite: remove `DATABASE_URL`, restart. Your `bamdude.db.migrated` file still contains your original SQLite data — rename it back to `bamdude.db`.

---

## :material-lightbulb: Tips

!!! tip "Test with Docker Compose"
    Use the Docker Compose example above to test PostgreSQL locally before deploying to production.

!!! tip "Connection String Security"
    Avoid putting passwords in environment variables in production. Use Docker secrets or a `.env` file with restricted permissions.
