---
title: Upgrading & Migration
description: Migrate to BamDude from Bambuddy, move between install methods, and update routinely.
---

# Upgrading & Migration

This guide covers three scenarios:

1. **Migrating to BamDude** from Bambuddy (or Bambuddy-HE) for the first time.
2. **Switching install method** (self-install ↔ Docker ↔ GitHub Container Registry).
3. **Routine updates** between BamDude versions.

> **Always back up `data/` (or the `bamdude_data` / `bambuddy_data` volume) before any migration.** The DB schema advances forward only — there is no built-in downgrade path.

---

## :material-information-outline: Supported source versions

| Source | Status | Notes |
|---|---|---|
| **Bambuddy 2.2.2** | ✅ Fully supported & tested | The fork point. `m000_bambuddy_import` migration handles this path automatically. |
| **Bambuddy-HE / BamDude 0.2.x** | ✅ Supported | Docker volume prefix changed from `bambuddy_he_*` → `bamdude_*`. Use `install/migrate-volumes.sh` (Docker) or rename `bambuddy.db` → `bamdude.db` (native). |
| **BamDude 0.3.0.1 / 0.3.1.x / 0.3.2** | ✅ Supported | Routine update — no special steps. |
| **Bambuddy 0.2.3 or newer** | ⚠️ **Not tested, not supported** | Schemas diverged after the fork. The upstream audit protocol ports features selectively; the on-disk DB shape can differ in ways `init_db()` does not expect. See the warning below. |

!!! warning "Newer Bambuddy versions (0.2.3+)"
    BamDude is a hard fork that drifted from upstream starting at 2.2.2. Since then both projects have added migrations independently. Migrating *from* a newer Bambuddy build into BamDude has **never been tested** — tables may exist with shapes BamDude's auto-migrations don't know how to handle, or rows may reference IDs BamDude will try to rewrite.

    If you want to try anyway:

    1. Keep an unmodified copy of your Bambuddy `data/` directory — plan to throw the migrated BamDude DB away and start fresh if anything looks off.
    2. Open the UI and compare printers, archives, spool inventory, maintenance history against what you expect.
    3. Check logs for any `ERROR` from a migration helper (`backend.app.migrations.m00*`) on the first boot. Migrations log every schema change.

    File an issue if you hit a specific table you can't migrate — most cases are resolvable with a one-shot SQL step, but we don't ship that as a general-purpose tool.

---

## :material-shield-check: Before you begin — backup

Whatever path you choose, make a full backup first:

=== "Docker volumes"

    ```bash
    # Data volume (contains sqlite DB, archives, thumbnails, uploads)
    docker run --rm \
      -v bamdude_data:/from \
      -v "$(pwd)/backup":/to \
      alpine tar czf /to/bamdude-data-$(date +%Y%m%d).tar.gz -C /from .

    # Logs (optional)
    docker run --rm \
      -v bamdude_logs:/from \
      -v "$(pwd)/backup":/to \
      alpine tar czf /to/bamdude-logs-$(date +%Y%m%d).tar.gz -C /from .
    ```

=== "Native / self-install"

    ```bash
    cd /opt/bamdude     # or wherever you installed
    tar czf ~/bamdude-data-$(date +%Y%m%d).tar.gz data/
    ```

=== "Bambuddy (source project)"

    ```bash
    cd /path/to/bambuddy
    # The DB file is either bambuddy.db or bambutrack.db depending on version
    cp data/bambuddy.db ~/bambuddy-$(date +%Y%m%d).db
    tar czf ~/bambuddy-data-$(date +%Y%m%d).tar.gz data/
    ```

---

## :material-swap-horizontal: Scenario 1 — Migrating from Bambuddy 2.2.2

Place a Bambuddy DB file next to where BamDude expects to find it. On first boot the `m000_bambuddy_import` migration detects it, imports every table BamDude still uses, and renames the file to `bamdude.db`.

The original Bambuddy file is **left in place** (not deleted) so you can roll back.

### via Docker Compose (source checkout)

```bash
# 1. Stop Bambuddy
cd /path/to/bambuddy && docker compose down

# 2. Clone BamDude
git clone https://github.com/kainpl/bamdude.git
cd bamdude

# 3. Copy your Bambuddy DB + archives into the bamdude_data volume
docker volume create bamdude_data
docker run --rm \
  -v /path/to/bambuddy/data:/from \
  -v bamdude_data:/to \
  alpine cp -a /from/. /to/

# 4. Start — migrations run automatically on first boot
docker compose up -d

# 5. Follow startup logs, look for "Bambuddy → BamDude import complete"
docker compose logs -f bamdude
```

### via `docker run` (GHCR image)

```bash
# 1. Stop Bambuddy (however you run it)

# 2. Create the new volume and seed it with your Bambuddy data
docker volume create bamdude_data
docker run --rm \
  -v /path/to/bambuddy/data:/from \
  -v bamdude_data:/to \
  alpine cp -a /from/. /to/

# 3. Start BamDude from GHCR
docker run -d \
  --name bamdude \
  --network host \
  -e TZ=Europe/Kyiv \
  -v bamdude_data:/app/data \
  -v bamdude_logs:/app/logs \
  --restart unless-stopped \
  ghcr.io/kainpl/bamdude:latest
```

### via native / self-install

```bash
# 1. Stop the Bambuddy service

# 2. Install BamDude
curl -fsSL https://raw.githubusercontent.com/kainpl/bamdude/main/install/install.sh \
  -o install.sh && chmod +x install.sh
sudo ./install.sh --yes       # defaults to /opt/bamdude

# 3. Drop your Bambuddy DB into BamDude's data dir BEFORE first start
sudo cp /path/to/bambuddy/data/bambuddy.db /opt/bamdude/data/
sudo cp -r /path/to/bambuddy/data/archives /opt/bamdude/data/   # if you have one

# 4. Fix ownership (installer runs as the bamdude service user)
sudo chown -R bamdude:bamdude /opt/bamdude/data/

# 5. Start the service — import migration fires automatically
sudo systemctl start bamdude
sudo journalctl -u bamdude -f
```

!!! tip "The import is one-shot"
    `m000_bambuddy_import` checks for `bambuddy.db` / `bambutrack.db` and only runs if BamDude's own `bamdude.db` does not yet exist. After a successful import the file is renamed to `bamdude.db` and the migration is marked applied in the `_migrations` table, so a subsequent restart won't re-import.

---

## :material-swap-horizontal: Scenario 2 — Migrating from Bambuddy-HE / BamDude 0.2.x

Bambuddy-HE was the earlier name of this fork. At the 0.3.0.1 rebrand the Docker volumes were renamed from `bambuddy_he_*` to `bamdude_*`, and the SQLite file from `bambuddy.db` to `bamdude.db`. The schema itself is continuous — no data import is needed, just a rename.

### via Docker Compose — automated

The repo ships a one-shot migration script that copies the `bambuddy_he_*` volumes into `bamdude_*`:

```bash
cd bamdude
./install/migrate-volumes.sh
docker compose up -d
```

The script refuses to run if `bamdude_data` already exists; in that case either remove it (`docker volume rm bamdude_data bamdude_logs`) or do the copy manually as below.

### via Docker Compose — manual

```bash
# 1. Stop the old container
docker compose -f /path/to/bambuddy-he/docker-compose.yml down

# 2. Copy volumes
docker volume create bamdude_data
docker volume create bamdude_logs
docker run --rm -v bambuddy_he_data:/from -v bamdude_data:/to alpine cp -a /from/. /to/
docker run --rm -v bambuddy_he_logs:/from -v bamdude_logs:/to alpine cp -a /from/. /to/

# 3. Start — auto-rename of bambuddy.db → bamdude.db happens on first boot
docker compose up -d
```

### via native / self-install

No volume copying needed — just point BamDude at your existing data directory:

```bash
# 1. Stop the old service (however you ran Bambuddy-HE)

# 2. Install BamDude
sudo ./install/install.sh --data-dir /path/to/existing/data --yes

# 3. Start — the boot-time detector renames bambuddy.db → bamdude.db in place
sudo systemctl start bamdude
```

---

## :material-arrow-up-circle: Scenario 3 — Routine updates between BamDude versions

Once you're on BamDude, updates are schema-forward and non-destructive: new migrations apply automatically, nothing renames, nothing deletes.

### Docker Compose

```bash
cd bamdude
git pull
docker compose pull      # refresh the GHCR image
docker compose up -d     # restart with the new image
```

If you build from source instead of pulling:

```bash
docker compose up -d --build
```

### `docker run` (GHCR image)

```bash
docker pull ghcr.io/kainpl/bamdude:latest
docker stop bamdude && docker rm bamdude
docker run -d \
  --name bamdude \
  --network host \
  -e TZ=Europe/Kyiv \
  -v bamdude_data:/app/data \
  -v bamdude_logs:/app/logs \
  --restart unless-stopped \
  ghcr.io/kainpl/bamdude:latest
```

### Native / self-install — scripted

The installer ships an `update.sh` that takes a pre-update backup, pulls the latest code, rebuilds the frontend, reinstalls Python deps, and restarts the service:

```bash
sudo /opt/bamdude/install/update.sh
```

Environment overrides:

| Variable | Default | Purpose |
|---|---|---|
| `INSTALL_DIR` | `/opt/bamdude` | Where BamDude lives |
| `SERVICE_NAME` | `bamdude` | systemd unit to restart |
| `BRANCH` | current checked-out branch | Switch to another branch during update |
| `BACKUP_MODE` | `auto` | `auto` skips when nothing to back up, `require` aborts if backup fails, `skip` disables |
| `FORCE` | `0` | Set to `1` to bypass dirty-worktree / backup checks |

### Native / self-install — manual

```bash
cd /opt/bamdude
sudo systemctl stop bamdude
sudo -u bamdude git pull origin main
sudo -u bamdude ./venv/bin/pip install -r requirements.txt
sudo -u bamdude bash -c 'cd frontend && npm ci && npm run build'
sudo systemctl start bamdude
```

---

## :material-compare-horizontal: Switching install method

You can change install method at any time without touching data — just point the new instance at the existing `data/` directory or copy the volume contents.

### Native → Docker

```bash
sudo systemctl stop bamdude

# Copy native data into a Docker volume
docker volume create bamdude_data
docker run --rm \
  -v /opt/bamdude/data:/from \
  -v bamdude_data:/to \
  alpine cp -a /from/. /to/

# Start the GHCR image against the new volume
docker run -d --name bamdude --network host \
  -v bamdude_data:/app/data -v bamdude_logs:/app/logs \
  --restart unless-stopped ghcr.io/kainpl/bamdude:latest

# Only after you've verified the Docker instance works, disable/remove the native service:
sudo systemctl disable bamdude
```

### Docker → Native

```bash
docker compose down

# Copy the volume out to disk
docker run --rm \
  -v bamdude_data:/from \
  -v "$(pwd)/extracted":/to \
  alpine cp -a /from/. /to/

# Install native pointing at the extracted data
sudo ./install/install.sh --data-dir "$(pwd)/extracted" --yes
```

### Docker Hub → GHCR (or vice versa)

Registry swap only, no data touch:

```bash
# docker-compose.yml
# image: kainpl/bamdude:latest      ← Docker Hub
# image: ghcr.io/kainpl/bamdude:latest  ← GitHub Container Registry
docker compose pull
docker compose up -d
```

Both registries publish the same tags. GHCR is the preferred source (built in CI on every release); Docker Hub is a mirror.

---

## :material-database-check: What actually runs on first boot

When BamDude starts for the first time after an upgrade or migration, `init_db()` runs this sequence:

1. **Legacy detection** — if `bambuddy.db` or `bambutrack.db` exists but `bamdude.db` does not, the file is renamed.
2. **`create_all()`** — SQLAlchemy creates any tables the model code knows about that don't exist yet. Idempotent and safe to re-run.
3. **Auto-migrate SQLite → PostgreSQL** — only if `DATABASE_URL` points at Postgres *and* that database is empty.
4. **`_migrations` table** — created if missing. This is where applied-migration version numbers live.
5. **Bootstrap** — pre-migration installs get `m000` + `m001` pre-stamped so they don't re-run against already-migrated data.
6. **Pending migrations** — `m002`, `m003`, … run in order. Each is a single atomic `add_column` / `CREATE INDEX` / data fixup.

All of this happens without user action. The logs show each step:

```
INFO  [backend.app.core.database] Renamed legacy bambuddy.db → bamdude.db
INFO  [backend.app.migrations] Applying m007_drop_vibration_cali
INFO  [backend.app.migrations] Applied m007 (version 7)
```

---

## :material-alert-octagon: Rollback

Because the schema advances forward only, the rollback plan is always "restore the pre-upgrade backup":

=== "Docker volumes"

    ```bash
    docker compose down
    docker volume rm bamdude_data
    docker volume create bamdude_data
    docker run --rm \
      -v "$(pwd)/backup":/from \
      -v bamdude_data:/to \
      alpine sh -c 'cd /to && tar xzf /from/bamdude-data-YYYYMMDD.tar.gz'
    # Pin to the old image tag before starting:
    # image: ghcr.io/kainpl/bamdude:0.3.2
    docker compose up -d
    ```

=== "Native"

    ```bash
    sudo systemctl stop bamdude
    cd /opt/bamdude
    sudo rm -rf data && sudo tar xzf ~/bamdude-data-YYYYMMDD.tar.gz
    sudo -u bamdude git checkout v0.3.2     # or your prior tag
    sudo -u bamdude ./venv/bin/pip install -r requirements.txt
    sudo systemctl start bamdude
    ```

The version you roll back to must be the one that created the backup — otherwise the schema in the DB will be newer than what that code expects, and startup will fail with a column-not-found error on a table read.

---

## :material-bug: Troubleshooting

**Startup log shows `setup_required` 503s on every endpoint**
: First boot creates no admin. Open `/` in a browser to go through the setup flow. This is normal for fresh installs and after every `cli reset_admin`.

**`no such column` / `no such table` on startup**
: A migration did not run. Check the log for the stack trace; usually it means the file permissions on `data/` don't allow the service user to write. Fix with `sudo chown -R bamdude:bamdude /opt/bamdude/data`.

**Bambuddy import didn't fire**
: Either `bamdude.db` already exists (so the file was never scanned) or the file is not named `bambuddy.db` / `bambutrack.db`. Rename and restart — the migration check re-runs on every boot until applied.

**Docker volume copy fails with `device or resource busy`**
: Stop both the source and the destination container first. The `--rm` alpine container mounting both volumes cannot share the filesystem with a running service holding open files.

**Native update leaves the service broken**
: `update.sh` writes a backup before it touches anything (`/opt/bamdude/backups/pre-update-YYYYMMDD-HHMMSS/`). Stop the service, restore the backup directory over `data/`, and check out the prior git tag.

---

## :material-new-box: What's new in BamDude

| Feature | Description |
|---------|-------------|
| **Per-Printer Queues** | Independent queue per printer with card-based UI |
| **Staggered Start** | Roll out batch prints in groups to avoid power spikes |
| **Swap Mode** | A1 Mini plate swapper with profile selection and macro auto-execution |
| **Macros** | G-code macros triggered by print events |
| **Telegram Bot** | Full printer control from Telegram with inline menus + actionable notifications |
| **Multi-chat auth** | Per-chat roles, permissions, registration modes |
| **Maintenance history** | Model-aware types with Excel export |
| **Authentication** | Group-based permissions (80+), JWT, API keys, LDAP, Bambu Cloud |
| **3MF download recovery** | Fallback archives auto-fill via FTP when the printer was unreachable at print start |

See [CHANGELOG.md](https://github.com/kainpl/bamdude/blob/main/CHANGELOG.md) for the per-version detail.
