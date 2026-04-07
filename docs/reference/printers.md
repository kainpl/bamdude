---
title: Supported Printers
description: Compatibility information for Bambu Lab printer models
---

# Supported Printers

BamDude supports all Bambu Lab 3D printers with Developer Mode capability.

---

## :material-check-circle: Supported Models

| Model | Series | Camera | AMS |
|-------|--------|:------:|:---:|
| **X1** | X1 | :material-check: | :material-check: |
| **X1 Carbon** | X1 | :material-check: | :material-check: |
| **X1E** | X1 | :material-check: | :material-check: |
| **H2D** | H2 | :material-check: | :material-check: |
| **H2D Pro** | H2 | :material-check: | :material-check: |
| **H2C** | H2 | :material-check: | :material-check: |
| **H2S** | H2 | :material-check: | :material-check: |
| **P1P** | P1 | Add-on | :material-check: |
| **P1S** | P1 | :material-check: | :material-check: |
| **P2S** | P2 | :material-check: | :material-check: |
| **A1** | A1 | :material-check: | AMS Lite |
| **A1 Mini** | A1 | :material-check: | :material-close: |

---

## :material-printer-3d: Feature Highlights by Series

### X1 Series

- Chamber heating
- Up to 4 AMS units
- Full camera support (up to 30 FPS)

### H2 Series

- H2D / H2D Pro: Dual nozzle with L/R nozzle status
- H2C: 6-slot tool changer nozzle rack
- Chamber heating

### P1 Series

- P1P requires add-on camera
- Up to 4 AMS units
- Camera limited to ~5 FPS

### A1 Series

- A1: AMS Lite support
- A1 Mini: External spool only, primary target for [swap mode](../features/swap-mode.md)
- Camera limited to ~5 FPS

---

## :material-connection: Connection Requirements

All printers require:

- **Developer Mode** enabled (provides LAN access)
- **SD card** inserted (for file transfers)
- **Same network** as BamDude server

| Port | Protocol | Purpose |
|------|----------|---------|
| 8883 | MQTT/TLS | Printer communication |
| 990 | FTPS | File transfers |

> Originally based on [Bambuddy](https://github.com/maziggy/bambuddy) documentation.
