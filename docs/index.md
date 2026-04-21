---
title: Home
description: BamDude - Self-hosted 3D print farm management for Bambu Lab printers
hide:
  - navigation
  - toc
---

<style>
.md-typeset h1 { display: none; }
</style>

<div class="hero" markdown>

<div markdown>

# Your Farm.<br>Your Data.<br>Your Control.

**BamDude** is a self-hosted 3D print farm management system for Bambu Lab printers. Monitor your fleet in real-time, queue and schedule prints, automate workflows with macros, and control everything from a Telegram bot.

Hard fork of [Bambuddy](https://github.com/maziggy/bambuddy) with per-printer queues, swap mode, staggered start, Telegram bot, macros, maintenance history, and more.

<div class="stats-row" markdown>
  <span class="stat-badge" markdown>:material-printer-3d: Multi-Printer</span>
  <span class="stat-badge" markdown>:material-cloud-off-outline: Works Offline</span>
  <span class="stat-badge" markdown>:material-open-source-initiative: Open Source</span>
</div>

[Get Started :material-arrow-right:](getting-started/index.md){ .btn .btn-primary }
[View on GitHub :material-github:](https://github.com/kainpl/bamdude){ .btn .btn-secondary }

</div>

</div>

---

## :rocket: Quick Start

<div class="quick-start" markdown>

[:material-download: **Installation**<br><small>Get up and running in minutes</small>](getting-started/installation.md)

[:material-docker: **Docker**<br><small>One-command deployment</small>](getting-started/docker.md)

[:material-printer-3d: **Add Printer**<br><small>Connect your first printer</small>](getting-started/first-printer.md)

[:material-arrow-up-circle: **Upgrading**<br><small>Migrate from Bambuddy</small>](getting-started/upgrading.md)

</div>

---

## :sparkles: Features

<div class="feature-grid" markdown>

<div class="feature-card" markdown>
### :material-monitor-dashboard: Real-time Monitoring
Live printer status via WebSocket, MJPEG camera streaming, HMS error tracking, and AMS humidity/temperature monitoring.
</div>

<div class="feature-card" markdown>
### :material-clock-outline: Per-Printer Queues
Independent print queues per printer with drag-and-drop ordering, scheduled prints, staggered start, and swap mode for A1 Mini plate swappers.
</div>

<div class="feature-card" markdown>
### :material-archive: Print Archive
Automatic 3MF archiving with metadata extraction, 3D model preview, duplicate detection, and full-text search.
</div>

<div class="feature-card" markdown>
### :material-robot: Telegram Bot
Full printer control from Telegram with inline menus, multi-chat authorization, role-based permissions, and actionable notifications.
</div>

<div class="feature-card" markdown>
### :material-code-braces: Macros
G-code macros triggered by print events (start, end, pause). Built-in editor with per-printer and per-model configuration.
</div>

<div class="feature-card" markdown>
### :material-bell-ring: Notifications
Multi-provider alerts via WhatsApp, Telegram, Discord, Email, Pushover, ntfy, and custom webhooks. Quiet hours and daily digest support.
</div>

</div>

[Explore All Features :material-arrow-right:](features/index.md){ .md-button }

---

## :printer: Supported Printers

| Series | Models |
|--------|--------|
| **X1 Series** | X1, X1 Carbon, X1E |
| **H2 Series** | H2D, H2D Pro, H2C, H2S |
| **P1 Series** | P1P, P1S |
| **P2 Series** | P2S |
| **A1 Series** | A1, A1 Mini |

---

## :wrench: Tech Stack

<div class="feature-grid" markdown>

<div class="feature-card" markdown>
### Backend
:material-language-python: Python
:material-api: FastAPI
:material-database: SQLAlchemy + SQLite
</div>

<div class="feature-card" markdown>
### Frontend
:material-react: React
:material-language-typescript: TypeScript
:material-tailwind: Tailwind CSS
</div>

<div class="feature-card" markdown>
### Communication
:material-transit-connection-variant: MQTT over TLS
:material-folder-network: FTPS
:material-web: WebSocket
</div>

</div>

---

<div style="text-align: center; margin-top: 3rem;" markdown>
<span style="opacity: 0.6;">Made with :heart: for the 3D printing community</span>
</div>
