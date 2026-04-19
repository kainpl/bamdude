---
title: Upgrading
description: Upgrade from Bambuddy or previous BamDude versions
---

# Upgrading

This guide covers upgrading from Bambuddy to BamDude, and updating between BamDude versions.

---

## :material-swap-horizontal: Migrating from Bambuddy 2.x

BamDude is a hard fork of Bambuddy. Your existing database and settings are compatible.

### Docker Migration

1. **Stop Bambuddy:**

    ```bash
    cd bambuddy
    docker compose down
    ```

2. **Backup your data:**

    ```bash
    cp -r bambuddy_data bambuddy_data_backup
    ```

3. **Clone BamDude:**

    ```bash
    git clone https://github.com/kainpl/bamdude.git
    cd bamdude
    ```

4. **Copy your data volumes:**

    Point the new `docker-compose.yml` volumes to your existing Bambuddy data, or copy the data directory:

    ```bash
    docker volume create bamdude_data
    docker run --rm -v bambuddy_data:/from -v bamdude_data:/to alpine cp -a /from/. /to/
    ```

5. **Start BamDude:**

    ```bash
    docker compose up -d
    ```

6. **Verify:** Open [http://localhost:8000](http://localhost:8000) and check that your printers and archives appear.

!!! warning "Backup First"
    Always backup your data before migrating. The migration is one-way -- BamDude may apply database migrations that are not backward-compatible with Bambuddy.

### Manual (Python) Migration

1. Stop the Bambuddy service
2. Backup `bambuddy.db` and the `archive/` directory
3. Clone BamDude and set up the venv
4. Copy `bambuddy.db` to the BamDude directory
5. Copy `archive/` to the BamDude directory
6. Start BamDude -- database migrations run automatically on startup

---

## :material-arrow-up-circle: Updating BamDude

### Docker

```bash
docker compose pull && docker compose up -d
```

### Manual (Python)

```bash
cd bamdude
git pull origin main
source venv/bin/activate
pip install -r requirements.txt
```

Then restart the service:

```bash
sudo systemctl restart bamdude
```

---

## :material-new-box: What's New in BamDude

BamDude adds these features on top of Bambuddy:

| Feature | Description |
|---------|-------------|
| **Per-Printer Queues** | Independent queue per printer with card-based UI |
| **Staggered Start** | Roll out batch prints in groups to avoid power spikes |
| **Swap Mode** | A1 Mini plate swapper support with swap files and macros |
| **Macros** | G-code macros triggered by print events |
| **Telegram Bot** | Full printer control from Telegram with inline menus |
| **Multi-Chat Auth** | Per-chat roles, permissions, and registration modes |
| **Maintenance History** | Detailed maintenance logging with model-aware types |
| **Authentication** | Granular role-based access control (80+ permissions) |

---

## :material-database: Database Compatibility

- BamDude uses the same SQLite database format as Bambuddy
- Database migrations run automatically on first startup
- No manual SQL required
- Existing printers, archives, settings, and queue items are preserved

!!! tip "Check Logs"
    After upgrading, check the logs for any migration messages:

    ```bash
    docker compose logs --tail 50 bamdude
    ```
