---
title: AMS & Humidity
description: Monitor AMS filament systems, humidity, and remote drying
---

# AMS & Humidity Monitoring

BamDude provides comprehensive monitoring for your AMS (Automatic Material System) units.

---

## :material-tray-full: AMS Slot Status

Each AMS slot displays:

- **Filament color** -- Visual color swatch
- **Material type** -- PLA, PETG, ABS, etc.
- **Remaining** -- Estimated filament left
- **Active** -- Currently feeding indicator
- **Slot number** -- 1-based number with auto-contrast text

### RFID Re-read

Refresh filament information for individual slots by hovering and clicking the menu button. Useful when you've swapped a spool but the AMS hasn't detected the change.

### Configure AMS Slot

Manually configure slots for third-party filaments:

1. Hover over a slot, click the menu
2. Select **Configure Slot**
3. Choose a filament preset (filtered by printer model)
4. Select a matching K profile
5. Optionally set a custom color

!!! tip "AMS-HT preset stickiness fixed (#1053)"
    Earlier builds keyed AMS-HT slot presets at `ams_id * 4 + tray_id = 512`, but the frontend looks them up by `ams_id` directly for HT (single-slot units share their global tray id with the unit id). The slot fell through to the generic preset (`Generic PLA`) on every poll even after a custom preset was saved — so operators had to re-select it after every spool change. Backend now keys via the same helper the frontend uses, and the saved preset stays put.

### Multi-AMS Support

Up to 4 AMS units per printer (16 total slots). External spool holders supported for printers without AMS.

### Assign Spool from Inventory

The AMS slot menu's **Assign Spool** option pairs a physical inventory row (from [Filaments](spoolman.md)) with the slot. The picker now includes:

- **RFID-detected spools** — Bambu Lab tags read on the slot.
- **Manually-added inventory rows without RFID** — refills, third-party brands, untagged spools (#1047). Earlier builds required exact `slicer_filament_name` equality and hid every spool that didn't carry a slicer profile name; the picker now also accepts partial-material match (a `PLA` spool shows up for a `PLA Basic` slot, and vice versa).
- **External slots (`amsId 254/255`)** — those have no RFID reader so the picker shows the full inventory.

!!! tip "Filter by slicer filament name"
    When a 3MF is loaded on the printer, the picker can be filtered by the slicer's expected filament profile (extracted from the active 3MF). Narrows the list to spools that match the print's required material — drops the chance of accidentally assigning a wrong spool. Toggle off the filter to see the full list, with a one-line warning when material doesn't match.

---

## :material-water-percent: Humidity Monitoring

| Level | Status | Action |
|:-----:|--------|--------|
| < 20% | :material-check-circle:{ style="color: #4caf50" } Excellent | None needed |
| 20-40% | :material-check-circle:{ style="color: #8bc34a" } Good | None needed |
| 40-60% | :material-alert:{ style="color: #ff9800" } Fair | Consider drying |
| > 60% | :material-alert-circle:{ style="color: #f44336" } High | Replace desiccant |

Configure custom warning thresholds in **Settings** > **General**.

---

## :material-fire: Remote AMS Drying

Control AMS drying directly from BamDude for AMS 2 Pro and AMS-HT units.

### Starting a Drying Session

1. Click the :material-fire: flame icon in the AMS card header
2. Select filament type, temperature, and duration
3. Optionally enable spool rotation
4. Click **Start**

### Queue Auto-Drying

Automatically dry filament between scheduled prints when humidity exceeds the threshold.

- Enable in **Settings** > **AMS Display Thresholds** > **Queue Auto-Drying** (`queue_drying_enabled`).
- **Non-blocking** (default, `queue_drying_block=false`) — drying runs in the background; prints in the queue take priority.
- **Blocking** (`queue_drying_block=true`) — the queue stalls until drying completes. Use this when you really want a dry spool before the next print starts and don't mind the wait.
- Per-filament temperature + duration come from the configurable presets (Settings → Print Queue), not hard-coded defaults — AMS 2 Pro and AMS-HT have separate columns since they reach different temperatures.

### Ambient Drying

A separate path that doesn't depend on the queue. Enable under **Settings** > **Print Queue** > **Ambient Drying** (`ambient_drying_enabled`). On any idle printer where humidity is above the threshold, BamDude starts drying without setting a target temperature — useful as a 24/7 humidity-keeper for an idle farm.

---

## :material-chart-line: Historical Charts

Click humidity or temperature indicators to view historical data with time ranges from 6 hours to 7 days, including min/max/avg statistics.

---

## :material-lightbulb: Tips

!!! tip "Auto-Drying Between Prints"
    Enable queue auto-drying to keep filament dry during long print queues, or enable ambient drying for all idle printers.

!!! tip "Desiccant Maintenance"
    When humidity consistently stays high, replace or regenerate your desiccant packets.

> Originally based on [Bambuddy](https://github.com/maziggy/bambuddy) documentation.
