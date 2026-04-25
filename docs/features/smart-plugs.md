---
title: Smart Plugs
description: Tasmota, Home Assistant, MQTT, and REST/Webhook power control with per-print energy tracking
---

# Smart Plugs

Control your printers with Tasmota, Home Assistant, REST/Webhook, or MQTT smart plugs for power monitoring, automation, and per-print energy tracking.

---

## :material-power-plug: Overview

Smart plug integration enables:

- **Power control** -- Turn printers on/off remotely
- **Energy monitoring** -- Track power consumption (lifetime + per-print)
- **Auto power-on** -- Start printer before scheduled prints
- **Auto power-off** -- Shut down after cooldown

---

## :material-cog: Supported Types

| Type | Control | Energy | Description |
|------|:-------:|:------:|-------------|
| **Tasmota** | :material-check: | :material-check: | Direct control of Tasmota-flashed plugs |
| **Home Assistant** | :material-check: | :material-check: | Any switch/light entity through HA |
| **REST / Webhook** | :material-check: | :material-check: | Custom HTTP API endpoints |
| **MQTT** | :material-close: | :material-check: | Monitor-only energy tracking |

---

## :material-robot: Automation

### Auto Power On

When a queued print is ready, BamDude turns on the plug, waits for the printer to boot, then starts the print.

### Auto Power Off

After a print completes, BamDude waits for bed cooldown, checks for more queued prints, then powers off.

Configure in **Settings → Smart Plugs** with cooldown temperature and time settings.

---

## :material-flash: Per-Print Energy Tracking

For smart plugs that report a kWh meter, BamDude captures the meter reading at print start and again on print complete. The delta is the energy consumed by **that specific print**, persisted on the archive row.

**How it works**

- Print starts → BamDude reads the plug's current kWh and writes it to `PrintArchive.energy_start_kwh`.
- Print completes → a fresh DB session re-reads the plug, computes `current - energy_start_kwh`, and stores the result on the archive.
- Failed and cancelled prints record partial energy — the delta from start to abort is still meaningful.

**Restart resilience**

`energy_start_kwh` is persisted on the archive row, never held in an in-memory dict. Restarting BamDude mid-print preserves the starting baseline and the print-end handler still produces the correct delta.

**Lifetime aggregation**

For "kWh used by printer X this month" reports, BamDude takes hourly snapshots in the `smart_plug_energy_snapshots` table. Date-range totals compute per-plug `max(0, last_in_range - baseline)` so meter resets (firmware reflashes, plug power cycles) don't cause negative deltas.

### Energy Display Mode

In **Settings → System → "Energy display mode"**:

| Mode | Source | Use Case |
|------|--------|----------|
| `print` | Sum of per-print archive deltas | "How much electricity did the prints I ran cost?" |
| `total` (default) | Lifetime plug counter via snapshot range | "How much did the printer's plug consume?" |

The two modes can diverge when the printer is powered (heating, idle, standby) without an active print — only `total` captures that.

---

## :material-wrench-cog: Reliability and Maintenance

Smart plugs are auto-resubscribed to MQTT on every BamDude startup via the `subscribe_plug_to_mqtt` helper. Most "plug stopped responding" cases are fixed by a single restart.

**If a plug doesn't respond after restart**

1. Confirm the plug works in its native app (Tasmota web UI / HA dashboard / Tasmota Console).
2. Check the plug's MQTT topic config in **Settings → Smart Plugs** — the per-type fields must match the plug's actual broker topics.
3. Restart BamDude one more time after fixing the topic.

The startup-restore code path, the create route, and the update route all funnel through the same helper, so the topic configuration can't drift between create and reconnect.

---

## :material-lightbulb: Tips

!!! tip "Start Simple"
    Start with manual power control before enabling automation.
    Build confidence in the plug's reliability before letting BamDude
    auto-cycle power.

!!! tip "Test Cooldown"
    Monitor a few prints to find the right cooldown temperature for
    your printer.

!!! tip "Pair with macros"
    Combine auto power-off with a `print_finished` macro to turn off
    chamber lights at the same time. See [Macros](macros.md).

!!! tip "Energy reporting"
    Switch the energy display mode to `print` if you charge customers
    per-print. Use `total` for personal "what does my farm cost"
    reporting — it includes idle/standby draw that `print` misses.

> Originally based on [Bambuddy](https://github.com/maziggy/bambuddy) documentation.
