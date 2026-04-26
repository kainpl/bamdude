# Updating BamDude

Short version. The full guide — including migration from Bambuddy, switching install method, and rollback — now lives in the separate docs repository at [`kainpl/docs.bamdude.top`](https://github.com/kainpl/docs.bamdude.top/blob/main/docs/getting-started/upgrading.md) ([українською](https://github.com/kainpl/docs.bamdude.top/blob/main/docs/getting-started/upgrading.uk.md)). Once `https://docs.bamdude.top/` goes live the links will become direct.

> **Always back up `data/` (or the `bamdude_data` Docker volume) before any upgrade.** The DB schema advances forward only — there is no built-in downgrade path. See the Rollback section of the [upgrade guide](https://github.com/kainpl/docs.bamdude.top/blob/main/docs/getting-started/upgrading.md#rollback) for the reverse procedure.

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
# ghcr.io/kainpl/bamdude:0.4.0          → pinned release
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

## Migrating FROM Bambuddy (source project)

See [`upgrading.md → Scenario 1`](https://github.com/kainpl/docs.bamdude.top/blob/main/docs/getting-started/upgrading.md#scenario-1-migrating-from-bambuddy-222). Short form: point BamDude at your existing Bambuddy `data/` directory, first boot runs `m000_bambuddy_import`, Bambuddy file is renamed (not deleted) so rollback is possible. Only Bambuddy **2.2.2** is tested; newer Bambuddy releases (0.2.3+) are untested and may break — the fork has diverged.

## Switching install method

Native ↔ Docker ↔ GHCR/Docker Hub swaps don't touch data — just point the new instance at the existing `data/` directory or copy the volume contents. Full commands in [`upgrading.md → Switching install method`](https://github.com/kainpl/docs.bamdude.top/blob/main/docs/getting-started/upgrading.md#switching-install-method).
