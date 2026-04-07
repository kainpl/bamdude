---
title: First Printer Setup
description: Connect your first Bambu Lab printer to BamDude
---

# First Printer Setup

This guide walks you through adding your first printer to BamDude.

---

## :material-list-status: Prerequisites

Before adding a printer, ensure:

- [x] BamDude is running ([Installation](installation.md) or [Docker](docker.md))
- [x] Your printer is powered on and connected to your network
- [x] **SD card is inserted** in the printer
- [x] Developer Mode is enabled ([see guide](index.md#enabling-developer-mode))
- [x] You have the IP address, serial number, and access code

---

## :material-printer-3d-nozzle: Adding Your Printer

### Step 1: Open the Add Printer Dialog

1. Open BamDude in your browser
2. Go to the **Printers** page
3. Click the **:material-plus: Add Printer** button

### Step 2: Enter Printer Details

| Field | Description | Example |
|-------|-------------|---------|
| **Name** | A friendly name for your printer | `Workshop X1C` |
| **IP Address** | Your printer's local IP address | `192.168.1.100` |
| **Access Code** | 8-character code from Developer Mode | `12345678` |
| **Serial Number** | Your printer's serial number | `01P00A000000001` |

### Step 3: Save and Connect

1. Click **Save**
2. BamDude will attempt to connect to your printer
3. Wait for the connection indicator to turn green

---

## :material-check-circle: Verifying Connection

A successfully connected printer shows:

| Element | Description |
|---------|-------------|
| :material-circle:{ style="color: #4caf50" } Green indicator | Connection is active |
| Live temperatures | Nozzle, bed, and chamber readings |
| HMS status | Health Management System shows "OK" |

---

## :material-alert-circle: Connection Issues?

1. **Is Developer Mode enabled?** Re-check both LAN Only Mode and Developer Mode
2. **Correct IP address?** Verify in printer network settings
3. **Access code fresh?** Codes change when Developer Mode is toggled
4. **Same network?** BamDude server and printer must be on the same LAN
5. **Firewall rules?** Ensure ports 8883 (MQTT) and 990 (FTPS) are open

See the full [Troubleshooting Guide](../reference/troubleshooting.md) for more.

---

## :material-printer-3d: Adding More Printers

Repeat the process to add additional printers. BamDude supports unlimited printers -- manage your entire print farm from one interface.

---

## :checkered_flag: Next Steps

<div class="quick-start" markdown>

[:material-archive: **Print Archiving**<br><small>How archiving works</small>](../features/archiving.md)

[:material-clock-outline: **Print Queue**<br><small>Schedule prints</small>](../features/print-queue.md)

[:material-bell-ring: **Notifications**<br><small>Get alerts on your phone</small>](../features/notifications.md)

[:material-robot: **Telegram Bot**<br><small>Control from Telegram</small>](../features/telegram-bot.md)

</div>

> Originally based on [Bambuddy](https://github.com/maziggy/bambuddy) documentation.
