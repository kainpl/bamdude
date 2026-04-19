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

### Multi-AMS Support

Up to 4 AMS units per printer (16 total slots). External spool holders supported for printers without AMS.

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

- Enable in **Settings** > **AMS Display Thresholds** > **Queue Auto-Drying**
- Non-blocking (default): prints take priority
- Blocking: queue waits for drying to finish

### Ambient Drying

Automatically dry filament on any idle printer, regardless of scheduled prints.

- Enable in **Settings** > **Print Queue** > **Ambient Drying**

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
