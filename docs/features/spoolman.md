---
title: Spoolman Integration
description: Sync filament inventory with Spoolman
---

# Spoolman Integration

Sync your AMS filament with [Spoolman](https://github.com/Donkie/Spoolman) for complete spool tracking and inventory management.

---

## :material-spool: What is Spoolman?

Spoolman is a self-hosted filament inventory manager that tracks spool quantities, records usage, and manages materials and vendors.

BamDude syncs your AMS slots with Spoolman for unified tracking.

---

## :material-link: Connection Setup

1. Go to **Settings** > **Integrations**
2. Find **Spoolman** section
3. Enter your Spoolman URL (e.g., `http://192.168.1.50:7912`)
4. Click **Test Connection**
5. Click **Save**

---

## :material-sync: Sync Features

- **AMS slot sync** -- Filament changes in AMS automatically update Spoolman
- **Usage tracking** -- Print filament consumption is recorded
- **Bi-directional** -- Changes in Spoolman reflect in BamDude

---

## :material-lightbulb: Tips

!!! tip "Network Access"
    Ensure BamDude can reach your Spoolman instance over the network. Both must be on the same LAN or accessible via routing.

> Originally based on [Bambuddy](https://github.com/maziggy/bambuddy) documentation.
