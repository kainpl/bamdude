---
title: Virtual Printer
description: Emulate a Bambu printer for slicer uploads, with review/auto-dispatch routes and PASV-NAT support
---

# Virtual Printer

The Virtual Printer (VP) makes BamDude appear as one or more Bambu Lab printers on your LAN. Bambu Studio / OrcaSlicer's "Send to Printer" lands files on a VP exactly the way it would on a real printer — over secure TLS (MQTT + FTPS) with the printer's access code. From there BamDude routes the upload according to the VP's mode.

---

## :material-printer-3d: Overview

Each VP:

- Advertises itself over **SSDP** with a real Bambu model code (X1C / P1S / A1 Mini / H2D / …) so slicers discover it automatically.
- Runs its **own FTPS + MQTT + SSDP servers** bound to a dedicated IP. Multiple VPs run side-by-side without port conflicts because each has its own IP, not its own port.
- Carries an **access code** like a real printer — slicers prompt for it on first use and cache it afterwards.
- Has a **serial number** and **model code** that match Bambu's real format, so the slicer's compatibility checks pass.

---

## :material-swap-horizontal: Modes

| Mode | What happens to uploads | Use case |
|------|-------------------------|----------|
| **immediate** | File is parsed and a `print_archives` row is created right away. Nothing prints. | Pure print archive — slicer is the source, BamDude is the catalogue. |
| **file_manager** | File lands in the **library** (`/library`) for later use. | Building a library you'll dispatch from later, manually or via the queue. |
| **print_queue** | File is archived **and** added to the print queue. Auto-dispatch + a target printer make this a one-click workflow. | The most common production mode: slice → send → BamDude prints it. |
| **proxy** | TCP-proxied straight to a real printer behind BamDude's TLS endpoint. | Remote printing — your slicer talks to BamDude over LAN/VPN, BamDude talks to the printer. |

---

## :material-cog: Setup

**Settings → Virtual Printer → Add Virtual Printer**:

| Field | Notes |
|-------|-------|
| Name | Display label (e.g. `Studio inbox`). |
| Model | SSDP model code — pick the printer model you want the VP to impersonate so slicer compatibility checks pass. |
| Bind IP | Dedicated IP for this VP. On Linux the simplest path is a virtual interface (alias) on the host. |
| Access code | 8-character code the slicer authenticates with. |
| Mode | One of the four above. |
| Auto-dispatch | `print_queue` mode only — see below. |
| Target printer | `proxy` mode only — the real printer to forward to. |

Slicers discover the new VP automatically via SSDP within a minute or two. If discovery fails, add it manually by IP + access code.

---

## :material-clipboard-check: Review Modal (file_manager mode)

In `file_manager` mode, every uploaded 3MF lands in a **review queue** at `/pending-uploads`. From the review modal an operator:

1. Opens an upload, sees the parsed metadata + thumbnail.
2. Picks the target real printer.
3. Verifies AMS slot mapping, plate selection, and any per-print options.
4. Clicks **Send to Printer** — the 3MF is dispatched through the standard background-dispatch pipeline (FTP upload, swap macros, archive linkage).

Review batches can also be **archived in bulk** (no print, just stash the metadata) or **rejected** (deletes the upload). Use this when multiple users / machines slice into the same VP and you want a sanity check before it actually hits a printer.

API: `GET /api/v1/pending-uploads/`, `POST /api/v1/pending-uploads/{id}/archive`, `POST /api/v1/pending-uploads/archive-all`.

---

## :material-flash: Auto-Dispatch (print_queue mode)

A VP in `print_queue` mode with `auto_dispatch=true` skips review entirely:

- Slicer "Send to Printer" → VP receives upload over FTPS
- VP creates a queue item targeting whichever real printer policy you've configured
- Background dispatch picks it up like any other queue item — no operator step

Set `auto_dispatch=false` if you want every queued upload to wait for an explicit Start click in the queue UI before dispatch.

!!! tip "Trusted upstream only"
    Auto-dispatch removes the human gate. Use it when the upstream source is yourself or a trusted automation (slicer plugin, CI job, MakerWorld webhook). For shared / multi-tenant uploads, prefer `file_manager` mode + the review modal.

---

## :material-network-outline: PASV Address (NAT / Docker bridge)

FTPS uses the PASV command, where the server tells the client which IP to dial back on for the data channel. When BamDude runs in a Docker bridge network (or behind any NAT), the PASV response would otherwise advertise the **container's internal IP** — slicers on the LAN can't reach it and the data channel fails mid-handshake.

Set the `VIRTUAL_PRINTER_PASV_ADDRESS` env var to the **externally-reachable IP** (the host's LAN address — most slicers don't resolve hostnames here):

```bash
VIRTUAL_PRINTER_PASV_ADDRESS=192.168.1.100
```

The FTPS server boots, logs `FTP PASV address override: 192.168.1.100`, and from then on every PASV reply uses that address. No effect when BamDude runs on the host network — leave it unset there.

---

## :material-rocket: Use Cases

- **Print archiving without printing** — `immediate` mode turns slice → send into a permanent record (thumbnails, metadata, source 3MF) without committing to a print.
- **Library building** — `file_manager` lands files in the library so you can attach them to projects, batch-print, or share with the team before the first build.
- **Multi-user farm inbox** — `file_manager` + review modal lets several people slice into the same VP without stepping on each other.
- **Hands-off dispatch** — `print_queue` + `auto_dispatch=true` is the closest you get to "Cloud Print but local".
- **Remote printing** — `proxy` mode forwards a remote slicer's TLS session straight to a real printer, with BamDude's certificate as the public face.

---

## :material-lightbulb: Tips

!!! tip "One VP per workflow"
    Nothing stops you running multiple VPs at once on different IPs — one for production auto-dispatch, one for review, one for archiving. They share the same backend so all data stays unified.

!!! tip "Slicer auth caching"
    Bambu Studio / OrcaSlicer cache the access code per discovered printer. Rotate the VP access code and slicers will prompt again — no manual cache-clear needed.

> Originally based on [Bambuddy](https://github.com/maziggy/bambuddy) documentation.
