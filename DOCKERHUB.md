# BamDude

**Self-hosted print archive, management and automation system for Bambu Lab 3D printers.**

Hard fork of [Bambuddy](https://github.com/maziggy/bambuddy) by maziggy, with Telegram bot, multi-chat auth, and print farm automation.

## Quick Start

### From Docker Hub

```bash
docker run -d \
  --name bamdude \
  --network host \
  -e TZ=Europe/Kyiv \
  -v bamdude_data:/app/data \
  -v bamdude_logs:/app/logs \
  --restart unless-stopped \
  kainpl/bamdude:latest
```

### From GitHub Container Registry

Same image, different registry — `ghcr.io/kainpl/bamdude:latest` is the CI-built mirror:

```bash
docker run -d \
  --name bamdude \
  --network host \
  -e TZ=Europe/Kyiv \
  -v bamdude_data:/app/data \
  -v bamdude_logs:/app/logs \
  --restart unless-stopped \
  ghcr.io/kainpl/bamdude:latest
```

### From source (docker compose)

```bash
git clone https://github.com/kainpl/bamdude.git
cd bamdude
docker compose up -d --build
```

Open **http://localhost:8000** and add your printer.

> **Requirements:** Bambu Lab printer with Developer Mode enabled, on the same local network.
>
> **macOS/Windows:** Docker Desktop doesn't support `--network host`. Use `-p 8000:8000` instead and add printers manually by IP.

## Features

- **Real-Time Monitoring** — Live printer status, camera streaming, HMS error tracking, multi-printer dashboard
- **Telegram Bot** — Full printer control, status, maintenance, queue from Telegram with multi-chat auth and actionable notifications
- **Print Archive** — Automatic 3MF archiving, 3D model viewer, timelapse editor, re-print with AMS mapping
- **Print Scheduling** — Per-printer queues, staggered start for farms, error-pause, clear plate confirmation
- **Smart Automation** — Smart plug control, auto power-on/off, energy monitoring, maintenance reminders
- **Notifications** — Telegram, Discord, Email, Pushover, ntfy with per-chat settings, quiet hours, actionable buttons
- **File Manager** — Upload sliced files, folder structure, external mounts, print directly
- **Integrations** — Spoolman, MQTT, Prometheus, Bambu Cloud, REST API, Home Assistant
- **Virtual Printer** — Archive, Review, Queue, or Proxy mode
- **Security** — Optional auth with 80+ granular permissions, JWT, API keys

## What's different from Bambuddy?

- Full Telegram bot (aiogram 3.x) with inline menus and reply keyboard
- Multi-chat authorization with per-chat roles and permissions
- Actionable notification buttons (clear plate, mark maintenance done)
- Per-chat notification events, quiet hours, daily digest
- Maintenance management from bot (view, mark done, edit hours)
- Ukrainian locale (full UI + bot + templates)
- Backend i18n system for bot translations
- MarkdownV2 Telegram formatting
- Various fixes: ghost prints, MQTT freshness, SD cleanup, server-side pagination

## Configuration

| Variable | Default | Description |
|---|---|---|
| `TZ` | `UTC` | Timezone (e.g. `Europe/Kyiv`) |
| `PORT` | `8000` | Web UI port |
| `DEBUG` | `false` | Enable debug logging |

## Volumes

| Path | Purpose |
|---|---|
| `/app/data` | Database, archived prints, thumbnails |
| `/app/logs` | Application logs |

## Docker Compose

```yaml
services:
  bamdude:
    build: .
    container_name: bamdude
    network_mode: host
    environment:
      - TZ=Europe/Kyiv
    volumes:
      - bamdude_data:/app/data
      - bamdude_logs:/app/logs
    restart: unless-stopped

volumes:
  bamdude_data:
  bamdude_logs:
```

## Upgrading & migration

Full manual (every source version × every install method, backup/rollback, troubleshooting):
**https://github.com/kainpl/bamdude/blob/main/docs/getting-started/upgrading.md**

Short version:

- **From Bambuddy 2.2.2** (tested & supported) — copy your Bambuddy data dir into the `bamdude_data` volume and start the container. The `m000` migration imports automatically and renames `bambuddy.db` → `bamdude.db`.
    ```bash
    docker volume create bamdude_data
    docker run --rm \
      -v /path/to/bambuddy/data:/from \
      -v bamdude_data:/to \
      alpine cp -a /from/. /to/
    ```
- **From Bambuddy-HE / BamDude 0.2.x** (tested & supported) — run the one-shot volume migration script, then start:
    ```bash
    bash install/migrate-volumes.sh   # copies bambuddy_he_* → bamdude_*
    docker compose up -d
    ```
- **Between BamDude versions** — routine `docker compose pull && docker compose up -d` (or `docker pull ghcr.io/kainpl/bamdude:latest` + recreate).
- **From Bambuddy 0.2.3 or newer** — ⚠️ **not tested.** BamDude diverged from upstream at 2.2.2 and applies its own migrations; newer upstream schemas may hit `no such column` on boot. Back up first and keep the Bambuddy data dir untouched so you can roll back.

## Telegram Bot Setup

1. Create a bot via [@BotFather](https://t.me/BotFather)
2. In Settings > Notifications, add Telegram provider with bot token
3. Enable Registration Mode, send `/start` to the bot
4. Assign a role to your chat in Settings > Telegram Chats
5. Control printers, view maintenance, confirm plate clears from Telegram

## Supported Printers

| Series | Models |
|---|---|
| H2 | H2C, H2D, H2D Pro, H2S |
| X1 | X1, X1 Carbon, X1E |
| P1 | P1P, P1S |
| P2 | P2S |
| A1 | A1, A1 Mini |

All printers require **Developer Mode** for LAN access.

## License

AGPL-3.0 License
