# Updating BamDude

Short version. The full guide — including migration from Bambuddy, switching install method, and rollback — lives at **<https://docs.bamdude.top/getting-started/upgrading/>** ([українською](https://docs.bamdude.top/uk/getting-started/upgrading/)).

> **Always back up `data/` (or the `bamdude_data` Docker volume) before any upgrade.** The DB schema advances forward only — there is no built-in downgrade path. See the Rollback section of the [upgrade guide](https://docs.bamdude.top/getting-started/upgrading/#7-rollback-if-things-break) for the reverse procedure.

---

## 1. Back up first

### UI (recommended for Native / self-install)

Open **Settings → Backup → Local Backup → Create Backup**, then **Download Backup** to save the zip to your computer. The zip packs the SQLite DB, archive directory, thumbnails, uploads, and config — the layout `install.sh` expects on disk, so restore is just "unzip into the install path and restart".

### Shell

```bash
# Native / self-install
cd /opt/bamdude
tar czf ~/bamdude-data-$(date +%Y%m%d).tar.gz data/

# Docker volumes
docker run --rm \
  -v bamdude_data:/from \
  -v "$(pwd)/backup":/to \
  alpine tar czf /to/bamdude-data-$(date +%Y%m%d).tar.gz -C /from .
```

---

## 2. Pull the new version

### Docker (recommended)

```bash
docker compose pull
docker compose up -d
```

The `:latest` tag tracks the `main` branch. To pin a specific release, edit your `docker-compose.yml`:

```yaml
# ghcr.io/kainpl/bamdude:latest         → always main
# ghcr.io/kainpl/bamdude:0.4.1          → pinned release
# kainpl/bamdude:latest                 → Docker Hub mirror (same bits)
```

### Native / self-install — scripted

```bash
sudo /opt/bamdude/install/update.sh
```

`update.sh` handles: stop service → backup (via UI API if reachable, else tar) → `git pull` → `pip install` → `npm ci && npm run build` → migrations run on first boot → start service. See the script header for env vars (`INSTALL_DIR`, `BRANCH`, `BACKUP_MODE`, `FORCE`).

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

## 3. Verify

Check the startup log for migration output:

```bash
# Docker
docker compose logs -f bamdude | head -200

# Native
sudo journalctl -u bamdude -f | head -200
```

You should see the `m00X` migrations that apply to the version you upgraded to, followed by the usual service-ready lines. `no such column` / `no such table` means a migration did not run — usually a filesystem permissions issue on `data/`; `sudo chown -R bamdude:bamdude /opt/bamdude/data` and restart.

---

## Switching the database backend

`DATABASE_URL` picks the storage and switching it is its own migration:

| `DATABASE_URL` | Backend |
|----------------|---------|
| *empty / unset* | SQLite at `data/bamdude.db` (default) |
| `embedded` | the PostgreSQL 18 bundled with BamDude, under `DATA_DIR/postgres/18` |
| `postgresql+asyncpg://…` | your own PostgreSQL server (the database must already exist) |

**SQLite → PostgreSQL** is a one-shot automatic copy. Back up first, set `DATABASE_URL`, restart. BamDude sees an empty PostgreSQL next to `bamdude.db`, copies every table across, then renames the file to `bamdude.db.migrated`:

```text
Found local SQLite database at .../bamdude.db, migrating to PostgreSQL
SQLite -> PostgreSQL migration complete (78 tables). Original renamed to bamdude.db.migrated
```

Two things to read in that log. PostgreSQL enforces foreign keys SQLite never did, so **unreachable orphan rows are purged and counted** (`Purging N orphan … rows`) — expected, but check the numbers look sane for your install. And a **failed import aborts the start**: PostgreSQL is left alone and `bamdude.db` is *not* renamed, so unsetting `DATABASE_URL` and restarting puts you back exactly where you were.

**Going back:** unset `DATABASE_URL` and `mv data/bamdude.db.migrated data/bamdude.db`. Anything written after the switch lives only in PostgreSQL — take a backup first if you want to keep it (the UI backup format restores onto either backend).

**Upgrading with the bundled server:** stop BamDude before `pip install -r requirements.txt`, or pip cannot replace the PostgreSQL package while a server from it is running (`update.sh` already stops the service first). Its cluster, password and port files live in `DATA_DIR/postgres/` — back them up with the rest of `data/`, service stopped. BamDude refuses to open a cluster from a different PostgreSQL major rather than touching it; a major bump ships as its own release with an explicit step.

Full guide, including the Windows service layouts and Docker: <https://docs.bamdude.top/features/postgresql/> and the [upgrade guide](https://docs.bamdude.top/getting-started/upgrading/).

---

## Migrating FROM Bambuddy (source project)

See [Scenario 1 in the upgrade guide](https://docs.bamdude.top/getting-started/upgrading/#scenario-1-migrating-from-bambuddy-222). Short form: point BamDude at your existing Bambuddy `data/` directory, first boot runs `m000_bambuddy_import`, Bambuddy file is renamed (not deleted) so rollback is possible. Only Bambuddy **2.2.2** is tested; newer Bambuddy releases (0.2.3+) are untested and may break — the fork has diverged.

## Switching install method

Native ↔ Docker ↔ GHCR/Docker Hub swaps don't touch data — just point the new instance at the existing `data/` directory or copy the volume contents. Full commands in [Switching install method](https://docs.bamdude.top/getting-started/upgrading/#switching-install-method).
