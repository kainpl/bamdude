---
title: Features
description: Explore all BamDude features
---

# Features

BamDude is packed with features to manage your 3D print farm. Explore them all below.

---

## :material-printer-3d: Printers & Monitoring

<div class="feature-grid" markdown>

<div class="feature-card" markdown>
### [:material-monitor-dashboard: Real-time Monitoring](monitoring.md)
Live printer status, temperatures, print progress, and HMS error tracking via WebSocket.
</div>

<div class="feature-card" markdown>
### [:material-camera: Camera Streaming](camera.md)
MJPEG live video streaming and snapshots from your printer's built-in camera.
</div>

<div class="feature-card" markdown>
### [:material-water-percent: AMS & Humidity](ams.md)
Monitor AMS slot status, humidity levels, and temperature. Remote drying and configurable presets.
</div>

</div>

---

## :material-clock-outline: Print Queue

<div class="feature-grid" markdown>

<div class="feature-card" markdown>
### [:material-printer-3d-nozzle: Per-Printer Queues](print-queue.md)
Independent queue per printer with drag-and-drop ordering, batch quantity, scheduled starts, and model-based assignment.
</div>

<div class="feature-card" markdown>
### [:material-timer-sand: Staggered Start](staggered-start.md)
Roll out batch prints in groups to avoid power spikes from simultaneous bed heating.
</div>

<div class="feature-card" markdown>
### [:material-swap-horizontal: Swap Mode](swap-mode.md)
A1 Mini plate swapper support with swap files, macros, and automatic plate clearing.
</div>

</div>

---

## :material-archive: Print Archive

<div class="feature-grid" markdown>

<div class="feature-card" markdown>
### [:material-archive-outline: Print Archiving](archiving.md)
Automatic 3MF archiving with metadata extraction, 3D preview, and duplicate detection.
</div>

<div class="feature-card" markdown>
### [:material-folder: File Manager](file-manager.md)
Browse, upload, and manage your local library of print files. Print directly or add to queue.
</div>

</div>

---

## :material-robot: Automation

<div class="feature-grid" markdown>

<div class="feature-card" markdown>
### [:material-power-plug: Smart Plugs](smart-plugs.md)
Tasmota, Home Assistant, REST/Webhook, and MQTT integration for auto power-on/off and energy monitoring.
</div>

<div class="feature-card" markdown>
### [:material-printer-3d: Virtual Printer](virtual-printer.md)
Emulate a Bambu printer on your network to send prints directly from your slicer.
</div>

<div class="feature-card" markdown>
### [:material-code-braces: Macros](macros.md)
G-code macros triggered by print events with a built-in editor.
</div>

<div class="feature-card" markdown>
### [:material-bell-ring: Notifications](notifications.md)
Multi-provider alerts via WhatsApp, Telegram, Discord, Email, and more.
</div>

</div>

---

## :material-send: Telegram Bot

<div class="feature-grid" markdown>

<div class="feature-card" markdown>
### [:material-robot: Bot Setup](telegram-bot.md)
Full printer control from Telegram with inline menus, print from library, and actionable notifications.
</div>

<div class="feature-card" markdown>
### [:material-shield-lock: Multi-Chat Auth](telegram-auth.md)
Per-chat authorization with roles, permissions, registration modes, and notification routing.
</div>

</div>

---

## :material-wrench: Maintenance & Settings

<div class="feature-grid" markdown>

<div class="feature-card" markdown>
### [:material-tools: Maintenance Tracker](maintenance.md)
Schedule and track maintenance tasks with interval reminders and detailed history.
</div>

<div class="feature-card" markdown>
### [:material-lock: Authentication](authentication.md)
Optional user authentication with role-based access control and 80+ granular permissions.
</div>

<div class="feature-card" markdown>
### [:material-backup-restore: Backup & Restore](backup.md)
Full database backup and restore for data protection.
</div>

</div>

---

## :material-puzzle: Integrations

<div class="feature-grid" markdown>

<div class="feature-card" markdown>
### [:material-spool: Spoolman](spoolman.md)
Sync filament inventory with Spoolman for complete spool tracking.
</div>

<div class="feature-card" markdown>
### [:material-wifi: MQTT Publishing](mqtt.md)
Publish events to external MQTT brokers for Home Assistant and Node-RED.
</div>

<div class="feature-card" markdown>
### [:material-chart-line: Prometheus Metrics](prometheus.md)
Export printer telemetry for Grafana dashboards and monitoring systems.
</div>

</div>
