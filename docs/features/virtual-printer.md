---
title: Virtual Printer
description: Emulate a Bambu printer to send prints from your slicer
---

# Virtual Printer

The Virtual Printer allows BamDude to emulate one or more Bambu Lab printers on your network. Send prints directly from Bambu Studio or OrcaSlicer to BamDude.

---

## :material-printer-3d: Overview

Each virtual printer:

- Appears as a real Bambu printer on your network (via SSDP discovery)
- Accepts print jobs over secure TLS connections (MQTT + FTP)
- Archives prints, queues them for review, or adds them to the print queue
- Runs on its own dedicated IP with independent services

---

## :material-swap-horizontal: Modes

| Mode | Description |
|------|-------------|
| **Immediate** | Files are archived automatically when received |
| **Review** | Files go to pending uploads for manual review |
| **Print Queue** | Files are archived AND added to the print queue |
| **Proxy** | Forwards traffic directly to a real printer (remote printing) |

---

## :material-cog: Setup

1. Go to **Settings** > **Virtual Printer**
2. Click **Add Virtual Printer**
3. Choose a bind IP address, printer model, and mode
4. Set an access code
5. Save and enable

The virtual printer appears in Bambu Studio / OrcaSlicer automatically via SSDP, or add manually by IP.

---

## :material-rocket: Use Cases

- **Print Archiving** -- Send prints to BamDude without starting them
- **Queue Building** -- Build up a print queue before printers are available
- **Remote Slicing** -- Slice on one computer, send to BamDude elsewhere
- **Remote Printing** -- Print from anywhere via Proxy Mode

---

## :material-lightbulb: Tips

!!! tip "Auto-Dispatch"
    In Print Queue mode, enable Auto-dispatch to start incoming prints automatically when a printer is available.

> Originally based on [Bambuddy](https://github.com/maziggy/bambuddy) documentation.
