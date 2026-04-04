# Bambuddy HE

**Self-hosted print archive, management and automation system for Bambu Lab 3D printers.**

Hard fork of [Bambuddy](https://github.com/maziggy/bambuddy) with Telegram bot, multi-chat auth, and print farm automation.

## Quick Start

```bash
git clone https://github.com/YOUR_REPO/bambuddy-he.git
cd bambuddy-he
docker compose up -d --build
```

Open **http://localhost:8000** and add your printer.

> **Requirements:** Bambu Lab printer with Developer Mode enabled, on the same local network.

## Features

- **Real-Time Monitoring** — Live printer status, camera streaming, HMS error tracking, multi-printer dashboard
- **Telegram Bot** — Full printer control, status, maintenance, queue from Telegram with multi-chat auth and actionable notifications
- **Print Archive** — Automatic 3MF archiving, 3D model viewer, timelapse editor, re-print with AMS mapping
- **Print Scheduling** — Drag-and-drop queue, model-based assignment, clear plate confirmation between prints
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
  bambuddy-he:
    build: .
    container_name: bambuddy-he
    network_mode: host
    environment:
      - TZ=Europe/Kyiv
    volumes:
      - bambuddy_data:/app/data
      - bambuddy_logs:/app/logs
    restart: unless-stopped

volumes:
  bambuddy_data:
  bambuddy_logs:
```

> **macOS/Windows:** Docker Desktop doesn't support `network_mode: host`. Replace with `ports: ["8000:8000"]` and add printers manually by IP.

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
