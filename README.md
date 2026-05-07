<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="static/img/bamdude_logo_dark.png">
    <source media="(prefers-color-scheme: light)" srcset="static/img/bamdude_logo_light.png">
    <img src="static/img/bamdude_logo_dark.png" alt="BamDude Logo" width="300">
  </picture>
</p>

<h1 align="center">BamDude</h1>

<p align="center">
  <strong>Self-hosted print archive, management and automation system for Bambu Lab 3D printers</strong>
  <br>
  <em>Hard fork of <a href="https://github.com/maziggy/bambuddy">Bambuddy</a> by maziggy, with Telegram bot, multi-chat auth, Ukrainian locale and more</em>
</p>

<p align="center">
  <a href="https://bamdude.top/"><img alt="Website" src="https://img.shields.io/badge/Website-bamdude.top-2dd4bf?style=flat-square&logoColor=white"></a>
  <a href="https://bamdude.top/features/"><img alt="Features" src="https://img.shields.io/badge/Features-overview-10b981?style=flat-square&logoColor=white"></a>
  <a href="https://docs.bamdude.top/"><img alt="Documentation" src="https://img.shields.io/badge/Docs-docs.bamdude.top-3b82f6?style=flat-square&logo=readthedocs&logoColor=white"></a>
  <a href="https://t.me/+3KQl2uNtOwo3NTgy"><img alt="Telegram Community" src="https://img.shields.io/badge/Telegram-BamDude%20Friends-26A5E4?style=flat-square&logo=telegram&logoColor=white"></a>
  <a href="https://hub.docker.com/r/kainpl/bamdude"><img alt="Docker Hub" src="https://img.shields.io/badge/Docker-Hub-2496ED?style=flat-square&logo=docker&logoColor=white"></a>
  <a href="https://github.com/kainpl/bamdude/releases"><img alt="Latest Release" src="https://img.shields.io/github/v/release/kainpl/bamdude?style=flat-square&logo=github"></a>
</p>

---

## What's different from Bambuddy?

BamDude is a hard fork of [Bambuddy](https://github.com/maziggy/bambuddy) focused on print farm operators who need deeper automation and Telegram-based control. Key additions:

- **Full Telegram bot** (aiogram 3.x) — printer control, status, maintenance, queue from Telegram
- **Multi-chat authorization** — each Telegram chat gets a role (group) with granular permissions
- **Actionable notifications** — "Clear plate", "Mark maintenance done" buttons right in Telegram notifications
- **Per-chat notification settings** — event types, quiet hours, daily digest per chat
- **Printer maintenance in bot** — view overdue items, mark done, edit hours
- **Clear plate from bot** — confirm plate cleared for queue auto-dispatch
- **Print from Library** — select file, pick printer (model-filtered), print now or add to queue
- **Queue management** — paginated list, detail, move, cancel, add to queue
- **Add Printer via bot** — enter IP, auto-detect serial/name/model via SSDP, enter access code
- **Camera snapshots** — `/camera` command and inline button per printer
- **Speed control** — change print speed mode from bot
- **Printer calibration** — model-aware UI and bot (bed leveling, vibration, motor noise, nozzle offset, high-temp)
- **Ukrainian locale** — full UI + bot + notification templates
- **Backend i18n system** — JSON-file-based translations for bot UI (easy to add languages)
- **MarkdownV2** — Telegram messages with proper formatting
- **Notification template editor** — MarkdownV2 toolbar with formatting buttons
- **Virtual Printer File Manager mode** — saves 3MF directly to library without archiving
- Various fixes: ghost print prevention, MQTT connection freshness, SD card cleanup, server-side pagination

---

## Why BamDude?

- **Own your data** — All print history stored locally, no cloud dependency
- **Works offline** — Uses Developer Mode for direct printer control via local network
- **Full automation** — Schedule prints, auto power-off, get notified when done
- **Multi-printer support** — Manage your entire print farm from one interface

---

## Features

<table>
<tr>
<td width="50%" valign="top">

### Print Archive
- Automatic 3MF archiving with metadata
- **3MF download recovery** — when the printer's FTP fails during archive, recovery triggers fire on startup / printer reconnect / print-complete / manual button; per-archive lock prevents duplicate FTP sessions
- **Per-plate awareness** — multi-plate prints record which plate of the source 3MF was actually printed; thumbnail, print info, G-code preview, and 3D model all reflect that plate (m038 backfills historical archives)
- 3D model preview (Three.js) with build-volume wireframe matching the printer's bed
- Duplicate detection & full-text search (source-hash chain-of-custody for patched files)
- Photo attachments & failure analysis
- Timelapse editor (trim, speed, music)
- Re-print to any printer with AMS mapping
- Archive comparison, tag management
- Print Log with filtering and pagination

### Monitoring & Control
- **Printer calibration** — bed leveling, vibration, motor noise, nozzle offset, high-temp heatbed (model-aware, from UI and Telegram bot)
- Real-time printer status via WebSocket
- Live camera streaming & snapshots — **fan-out broadcaster** so multiple browser tabs / HA cards / Frigate share a single upstream connection (the printer itself only allows one)
- Streaming overlay for OBS
- External camera support (MJPEG, RTSP, USB)
- Build plate empty detection
- Printer control (stop, pause, resume, light, speed)
- **Pause-state visualisation** — yellow status pip in compact mode, inline `Paused • {reason} · 14m` chip with live elapsed counter in card header (compact + expanded), instant WebSocket toast on RUNNING↔PAUSE edges with classified reason (door / filament runout / AI defect / plate-detect / etc.)
- AMS management (RFID re-read, slot config, drying)
- HMS error monitoring with history
- Print success rates, filament usage, cost analytics

### Scheduling & Automation
- Per-printer queues with status tracking (idle/printing/paused/error)
- Auto error-pause on print failure (queue stops, user decides next step)
- Staggered start for farms (limit concurrent heating, bed temp monitoring)
- **Swap Mode** — A1 Mini / A1 plate swapper with multi-profile support (Kit, STL, JobOx), auto-detect swap files, per-job event selection (start sequence / change table), plate-clear auto-bypass
- **Swap macro auto-execution** — `swap_mode_start` before print, `swap_mode_change_table` after print, with ACK + stg_cur completion tracking, queue pause on failure
- **Quick Vibration Check toggle** — per-job toggle; when disabled, 3MF gcode post-processor comments out `M970` commands, recalculates MD5 sidecars, repacks archive
- **Auto-Print G-code Injection** — per-job toggle that splices operator-defined snippets into the plate gcode at `; MACHINE_START_GCODE_END` (start) / EOF (end), with `{placeholder}` substitution from 3MF header (incl. PrusaSlicer→Bambu aliases). Snippets stored as per-printer-model JSON in settings; folded into the same single 3MF open/repack cycle as Quick Vibration Check so multi-plate 50+ MB files aren't unzipped twice
- **G-code macros** — execute from printer menu, ACK-based MQTT confirmation, `stg_cur` completion tracking, real-time status on printer card
- Model-aware maintenance types with history tracking and Excel export
- Clear plate confirmation between prints
- Smart plug integration (Tasmota, HA, MQTT, REST/webhook)
- Energy consumption tracking
- Auto power-on/off
- Background print dispatch with WebSocket progress

### File Manager (Library)
- Upload and organize sliced files
- **Composite file tags + chip-row filter** — `format` / `readiness` / `modifiers` / `provenance` chips drive both the badge row and a chip filter on the toolbar (sliced vs project vs raw geometry, single- vs multi-plate, MakerWorld provenance)
- **Per-plate gallery + 3D / G-code preview with build-volume wireframe** — multi-plate 3MFs expose every plate; library viewer hides tabs that don't apply to the file (e.g. no 3D tab for sliced `.gcode.3mf`); dual-handle layer slider (crop both top and bottom), travel-moves toggle, layer-play with 1× / 2× / 4× / 8× speeds, theme-synced canvas, wireframe / X-ray toggle, OBJ format support, Export-as-PNG
- External folder mounting (NAS, USB)
- STL / OBJ thumbnail generation — shaded surfaces with Lambertian lighting + transparent background so cards "float" on whatever theme is rendering them
- Folder structure with drag-and-drop
- Print directly or add to queue
- Duplicate detection
- **Trash bin with restore** — soft-delete with configurable retention (default 30 days), background sweeper hard-deletes past the window, opt-in scheduled auto-purge for old library files + archives; trash UIs render thumbnails and a unified split-button (trash + caret dropdown for purge-old)

### Projects
- Group related prints
- Track plates and parts
- **Print plan table**: per-file copies with live filament/time/cost totals + per-row printed/remaining counters
- **Headline "remaining" totals** on Print Jobs / Print Time / Filament Used cards (green when done, amber when there's work left)
- **One-click "Apply to project"** in print plan + BOM totals rows — writes plate count, parts count, and budget (filament + materials cost) into the project's target fields; project edit modal also pre-fills from the plan + shows a "From plan: N" hint to re-sync after changes
- Link folders or individual files from the File Manager — **many-to-many** (a file or folder can belong to several projects at once)
- Per-chip unlink (`×` on each project chip) for granular detach
- Import/Export as ZIP or JSON

</td>
<td width="50%" valign="top">

### Telegram Bot
- Full printer control: pause, resume, stop, light, speed, camera snapshot
- Printer calibration from bot: model-aware selection (bed leveling, vibration, motor noise, nozzle offset, high-temp heatbed)
- Printer status with model tag, maintenance indicators
- Edit printer hours, view/mark maintenance from bot
- Clear plate confirmation for queue auto-dispatch
- Print from Library: file → printer (model-filtered) → Print Now or Add to Queue
- Add to Queue: file → target (specific printer or model) → confirm
- Queue management: paginated list, detail, move up/down, cancel
- Add Printer: enter IP → SSDP auto-detect serial/name/model → access code → done
- Reply keyboard + inline menus + /start /status /camera /help commands
- Multi-chat auth with per-chat roles (BamDude permission groups)
- Per-chat notification events, quiet hours, daily digest
- Actionable notification buttons: clear plate, mark maintenance done, pause/stop on progress
- Auto-registration mode for new chats
- 13 handler modules, 171 i18n keys (EN/UK), MarkdownV2 formatting

### Notifications
- Telegram (auto-restart bot on config change), Discord, Email, Pushover, ntfy, CallMeBot
- Home Assistant, custom webhooks
- Customizable message templates (MarkdownV2 editor)
- Per-chat quiet hours & daily digest (Telegram)
- Actionable buttons: clear plate, mark maintenance done, pause/stop on progress
- Print finish photo, filament usage details
- HMS error alerts, bed cooled alerts
- **Pause / resume events with classified reason** — `print_paused` carries normalised `{reason}` (door / filament runout / presence-check / file-pause-command / AI defect / plate-objects / user) + matched `{hms_code}`; `print_resumed` carries `{paused_for}` (mm:ss). Default ON for new providers, included in default Telegram-chat event set
- Queue events (waiting, skipped, failed)

### Spool Inventory
- Built-in inventory with AMS slot assignment
- Automatic filament consumption tracking
- Per-spool cost tracking
- Bulk spool addition
- Spool catalog, color catalog, low-stock alerts
- Spoolman integration

### Integrations
- **Server-side slicing** — OrcaSlicer + BambuStudio sidecar containers ship in the same Compose project (`--profile orca` / `--profile bambu` / `--profile all`); per-job slicer picker in the Slice modal with live reachability badges, bed-type override (Cool / Engineering / High-Temp / Textured PEI / SuperTack), inline multi-plate selection, owner-filter on preset dropdowns
- Spoolman filament sync
- MQTT publishing for Home Assistant
- Prometheus metrics for Grafana
- Local OrcaSlicer preset import
- K-profiles (pressure advance)
- Git backup (GitHub + GitLab)
- API keys & webhooks
- LDAP/Active Directory authentication

### Virtual Printer & Remote Printing
- Proxy Mode for remote printing via TLS relay
- Archive, Review, Queue, **File Manager (NEW)**, or Proxy modes
- **File Manager mode** — saves received 3MF files to the library instead of archiving or printing
- SSDP discovery or manual IP
- **Tailscale Let's Encrypt cert** — opt-in per-VP toggle; when on, asks the local `tailscale` CLI for an LE cert and advertises the tailnet FQDN over SSDP so slicers connect via a hostname matching a trusted cert (no manual CA install)

### Authentication
- Group-based permissions (80+ granular)
- JWT tokens, API key support
- **OIDC (OpenID Connect)** — PocketID, Authentik, Keycloak, Authelia, Google, **Azure Entra ID** (`preferred_username` / `upn` claim, optional skip of `email_verified` gate)
- LDAP/Active Directory with group mapping
- Per-user Bambu Cloud accounts
- Advanced Auth via Email (SMTP)
- Per-user email notifications
- **Long-lived camera-stream tokens** — per-user, named, revocable tokens with hard 365-day TTL for Home Assistant cards, Frigate inputs, and wall-mounted kiosks

</td>
</tr>
</table>

**Plus:** Customizable themes, mobile responsive, multi-language (EN/UK), auto updates, database backup/restore, PostgreSQL support

---

## Quick Start

### Requirements
- Python 3.10+ (3.11/3.12 recommended)
- Bambu Lab printer with **Developer Mode** enabled
- Same local network as printer

### Docker Hub (Recommended)

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

Open **http://localhost:8000** in your browser.

> **macOS/Windows:** Docker Desktop doesn't support `--network host`. Use `-p 8000:8000` instead and add printers manually by IP.

### Docker Compose (from source)

```bash
git clone https://github.com/kainpl/bamdude.git
cd bamdude
docker compose up -d --build
```

### Manual Installation

```bash
git clone https://github.com/kainpl/bamdude.git
cd bamdude
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
```

### Upgrading or migrating

Full manual: **<https://docs.bamdude.top/getting-started/upgrading/>** ([source](https://github.com/kainpl/docs.bamdude.top)) — covers migration from Bambuddy 2.2.2, from Bambuddy-HE / BamDude 0.2.x, routine BamDude-to-BamDude updates, switching between self-install / Docker / GHCR, and rollback.

Short version:

- **From Bambuddy 2.2.2** (tested & supported) — drop `bambuddy.db` into BamDude's `data/` and start. The `m000` migration imports automatically and renames the file to `bamdude.db`.
- **From Bambuddy-HE / BamDude 0.2.x / 0.3.x** (tested & supported) — Docker users run `install/migrate-volumes.sh` once to copy `bambuddy_he_*` → `bamdude_*`; native users just point the installer at the existing data dir.
- **From Bambuddy 0.2.3 or newer** — ⚠️ not tested. BamDude diverged from upstream at 2.2.2 and applies its own migrations; newer upstream schemas may hit `no such column` errors on boot. Back up first, keep the Bambuddy data directory untouched, and file an issue if you hit a wall.

### Telegram Bot Setup

1. Create a bot via [@BotFather](https://t.me/BotFather), get the token
2. In BamDude Settings > Notifications, add a Telegram provider with the bot token
3. Enable Registration Mode, send `/start` to the bot from your Telegram
4. In Settings > Telegram Chats, assign a role to your chat and enable it
5. Done! Use the reply keyboard or inline menus to control printers

### Enabling Developer Mode

1. On printer: **Settings** > **Network** > **LAN Only Mode** > Enable
2. Enable **Developer Mode**
3. Note the **Access Code** and **IP address**

---

## Tech Stack

| Component | Technology |
|-----------|------------|
| Backend | Python, FastAPI, SQLAlchemy, aiogram 3.x |
| Frontend | React 19, TypeScript, Tailwind CSS 4 |
| Database | SQLite (default) or PostgreSQL |
| 3D Viewer | Three.js |
| Communication | MQTT (TLS), FTPS |
| Telegram | aiogram 3.x, MarkdownV2, FSM |

---

## Supported Printers

| Series | Models |
|--------|--------|
| X1 | X1, X1 Carbon, X1E |
| H2 | H2D, H2D Pro, H2C, H2S |
| P1 | P1P, P1S |
| P2 | P2S |
| A1 | A1, A1 Mini |

---

## Development

```bash
# Backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
DEBUG=true uvicorn backend.app.main:app --reload

# Frontend (separate terminal)
cd frontend && npm install && npm run dev
```

---

## License

AGPL-3.0 License — see [LICENSE](LICENSE) for details.

---

## Acknowledgments

- [Bambuddy](https://github.com/maziggy/bambuddy) by maziggy — the original project this is forked from
- [Bambu Lab](https://bambulab.com/) for amazing printers
- The reverse engineering community for protocol documentation
