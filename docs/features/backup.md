---
title: Backup & Restore
description: Manual ZIP backups, scheduled local backups, and Git-pushed profile backups
---

# Backup & Restore

Three independent paths protect your install: an on-demand ZIP from the UI, a scheduled local-disk job that keeps the last N snapshots, and a Git push that archives printer profiles to GitHub or GitLab.

---

## :material-backup-restore: What's in a Backup ZIP

The on-demand and scheduled local backups produce the same ZIP layout. Top-level entries:

| Entry | Contents |
|-------|----------|
| `bamdude.db` | The full database, **always exported as portable SQLite** — even when your runtime is PostgreSQL the dump goes through `dump_to_sqlite()` so the same ZIP restores onto either backend. |
| `archive/` | Every per-print archive directory: `.3mf`, thumbnail PNG, plate-N.png, and the per-archive folder. |
| `virtual_printer/` | Pending uploads + virtual-printer working state. |
| `plate_calibration/` | Reference frames + ROI definitions used by plate detection. |
| `icons/` | Custom icons uploaded for printers / projects. |
| `projects/` | Project attachments. |

Excluded by design: `logs/`, caches, temp files, the bundled frontend (it ships with the image / repo). Some sensitive fields are also filtered before the database dump — LDAP bind password is never returned in API responses, and API keys are stored as one-way hashes.

!!! note "PostgreSQL → SQLite → PostgreSQL"
    Even on a PostgreSQL runtime, `dump_to_sqlite()` normalises the export. Restoring on a fresh PostgreSQL install runs the inverse `import_sqlite_to_postgres()` and re-creates rows in the live database. The same ZIP also restores onto a SQLite install with no extra steps.

---

## :material-download: Manual Backup

1. **Settings → System → Backup & Restore**
2. Click **Create Backup**
3. Browser downloads `bamdude-backup-YYYYMMDD-HHMMSS.zip`

The ZIP is streamed from a temp file rather than buffered in memory, so multi-gigabyte backups don't OOM the process. The temp file is deleted automatically once the response finishes.

API: `GET /api/v1/settings/backup` (requires `settings:backup`).

---

## :material-clock-outline: Scheduled Local Backups

Set under **Settings → System → Local Backup Schedule**. The scheduler ticks once per minute and fires due jobs into the same ZIP builder the manual button uses, then prunes older backups beyond the retention limit.

| Setting | Default | Notes |
|---------|---------|-------|
| `local_backup_enabled` | `false` | Master switch. |
| `local_backup_schedule` | `daily` | `hourly`, `daily`, or `weekly`. |
| `local_backup_time` | `03:00` | `HH:MM` for daily/weekly runs (server-local time). Hourly ignores this. |
| `local_backup_retention` | `5` | Keep the most recent N backups; older ones auto-prune. Range 1–100. |
| `local_backup_path` | empty | Output directory. Empty = `data/backups/`. |

The settings page shows last-run timestamp + outcome (`success` / `failed`), the next scheduled run, and a list of currently retained backups with file sizes. Manual "Create Backup" runs are stored in the same directory and counted toward retention.

Legacy `bambuddy-backup-*.zip` files (from upstream installs) are still listed and restorable so an upgrade doesn't strand pre-existing snapshots.

---

## :material-source-branch: Git Backup (Profiles to GitHub / GitLab)

Distinct from the ZIP flow. **Settings → System → Git Backup** pushes selected printer-profile data to a GitHub or GitLab repository — useful for off-site profile sync, multi-host farm coordination, and PR-based change history of your printer settings.

### :material-cog-outline: Configuration

| Setting | Notes |
|---------|-------|
| Provider | `github` or `gitlab`. |
| Repository URL | Full clone URL (HTTPS form). |
| Access Token | Personal Access Token. Stored encrypted at rest. |
| Branch | Target branch (default `main`). |
| API base URL | Self-hosted GitLab only. |
| Schedule | `hourly` / `daily` / `weekly`, or off. |

### :material-checkbox-marked: What gets pushed

Toggle each independently:

- **K-profiles** — per-printer K-profile JSON.
- **Cloud profiles** — Bambu Cloud filament profiles per user.
- **Settings** — application settings table (sensitive fields excluded).
- **Spools** — full inventory dump.
- **Archives** — print history records.

Only changed files generate commits — a no-op run is recorded as `skipped`.

### :material-monitor-dashboard: Status panel

The settings page shows the live status:

- **Last backup** — timestamp, status (`success` / `failed` / `skipped`), commit SHA, and message.
- **Next scheduled run** — when the scheduler will fire next.
- **Log table** — historical runs with trigger (`manual` / `scheduled`), duration, and any error message.
- **Run Now** button — fires an immediate push regardless of schedule.

Push frequency, content checkboxes, and credentials can all be edited live without restarting BamDude.

---

## :material-upload: Restoring a Backup ZIP

1. **Stop BamDude** before restoring (or the upload below replaces files under a running process — risky).
2. Either drop the ZIP into the data directory and let BamDude detect it on next boot, or use **Settings → System → Restore** and upload through the form.
3. On boot / form submission, BamDude:
   - Extracts the ZIP into a temp dir
   - Closes the current DB connections
   - Replaces the database (`bamdude.db` import on SQLite, `import_sqlite_to_postgres` on PG)
   - Replaces `archive/`, `virtual_printer/`, `plate_calibration/`, `icons/`, `projects/`
   - Re-initialises the database (runs pending migrations on the restored data)
   - Deletes the source ZIP after success

!!! danger "Restore replaces current state"
    The restore overwrites the live DB and the data directories listed above. **Take a fresh backup of the current state first** if you might want to roll back the restore itself.

API: `POST /api/v1/settings/restore` (multipart `file=…`, requires `settings:restore`).

### :material-database-arrow-right: Cross-backend restore

The portable SQLite dump means you can:

- Take a backup from a **SQLite** install → restore onto **PostgreSQL** (the loader migrates rows).
- Take a backup from a **PostgreSQL** install → restore onto **SQLite** (DB was already exported as SQLite).
- Take a backup from PG → restore onto a fresh PG (loader re-imports SQLite into PG).

Conflicting primary keys are merged or skipped per row depending on the table — referential integrity is preserved across the migration.

---

## :material-lightbulb: Tips

!!! tip "Off-site coverage"
    Combine **Scheduled Local Backups** (full data, on-disk) with **Git Backup** (profiles, off-site) — the local one survives a software wipe, the git one survives a hardware loss.

!!! tip "Backup before upgrade"
    [`UPDATING.md`](https://github.com/kainpl/bamdude/blob/main/UPDATING.md) recommends a fresh manual backup before every minor-version upgrade. Migrations are idempotent and one-shot but a downgrade has no automatic path.

> Originally based on [Bambuddy](https://github.com/maziggy/bambuddy) documentation.
