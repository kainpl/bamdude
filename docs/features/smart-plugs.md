---
title: Smart Plugs
description: Tasmota, Home Assistant, MQTT, and REST/Webhook power control
---

# Smart Plugs

Control your printers with Tasmota, Home Assistant, REST/Webhook, or MQTT smart plugs for power monitoring, automation, and energy tracking.

---

## :material-power-plug: Overview

Smart plug integration enables:

- **Power control** -- Turn printers on/off remotely
- **Energy monitoring** -- Track power consumption
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

Configure in **Settings** > **Smart Plugs** with cooldown temperature and time settings.

---

## :material-lightbulb: Tips

!!! tip "Start Simple"
    Start with manual power control before enabling automation.

!!! tip "Test Cooldown"
    Monitor a few prints to find the right cooldown temperature for your printer.

> Originally based on [Bambuddy](https://github.com/maziggy/bambuddy) documentation.
