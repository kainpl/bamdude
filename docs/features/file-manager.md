---
title: File Manager
description: Browse and manage your local library of print files
---

# File Manager

Browse, upload, and manage files in your local BamDude library. Print directly or add to queue.

---

## :material-folder: Overview

The File Manager lets you:

- **Browse** files in your local library
- **Mount external folders** from NAS, USB, or network shares
- **Upload** files including ZIP archives
- **Print directly** to any printer
- **Add to Queue** sliced files for later printing
- **Rename** and **delete** files and folders

---

## :material-printer: Print Directly

1. Find a sliced file (`.gcode` or `.gcode.3mf`)
2. Click the printer icon or right-click for context menu
3. Select **Print**
4. Choose printer(s), configure filament mapping, set print options
5. Click **Print** to start

### Add to Queue

Queue sliced files for later printing without creating archives upfront. Archives are created automatically when the print starts.

---

## :material-folder-zip: ZIP File Uploads

Upload ZIP archives to extract contents into your library:

1. Click **Upload** and select a `.zip` file
2. Choose whether to preserve folder structure
3. Click **Extract**

---

## :material-database: How files are stored

Every file in the library is a row in the `library_files` table. The row carries:

- **Hash dedup** -- uploads are SHA-256'd and matched against existing rows; an identical re-upload returns the existing entry instead of creating a duplicate copy on disk.
- **Thumbnails** -- extracted from `Metadata/plate_*.png` inside the 3MF on upload (no on-the-fly extraction). Re-uploads or "reparse" trigger fresh extraction.
- **`print_count` + `last_printed_at`** -- usage counters maintained by dispatch; visible in the file-card hover and used by sort modes. Backfilled retroactively on upgrade by migration `m014`.
- **`file_metadata` JSON column** -- stores parsed slicer metadata: filament weights per spool, object count, sliced-for printer model, plus the new `gcode_label_objects` / `exclude_object` flags from the source 3MF's `Metadata/project_settings.config` (extracted in 0.4.1, backfilled by migration `m022`). The label-object flags gate the **skip-objects** button on the printer page during a print -- both must be `true` for the button to light up. Bambu Studio enables both by default; OrcaSlicer ships with both off (see [Troubleshooting](../reference/troubleshooting.md) for the slicer-side checklist).
- **`swap_compatible` flag** -- detected from the filename suffix (`_swap`, `_swap.gcode.3mf` and similar). Swap-compatible files are surfaced separately in the swap-mode picker.

---

## :material-link-variant: Project & Folder Links

- **Per-folder link** -- linking a folder to a project sets `project_id` on every file inside, and any file moved into that folder later inherits the link.
- **Per-file link** -- each file row also has its own `Link2` button to attach it to a project independently of its folder.
- **Per-project plan items** (m016) -- the project page renders a flat plan list with copies/order/totals; rows auto-appear when files / folders link to the project, and per-row totals (filament, time, cost) feed the project-level grand totals.

---

## :material-folder-network: External Folder Mounting

Mount host directories (NAS, USB drives) into the File Manager without copying files:

1. Bind-mount the directory into Docker
2. Click **Link External** in the toolbar
3. Enter display name and container path
4. Files are indexed and appear immediately

---

## :material-lightbulb: Tips

!!! tip "Multi-Printer Support"
    Select multiple printers to send the same file to your entire print farm at once.

!!! tip "File Badges"
    Look for "sliced" badges to identify files ready for printing.

> Originally based on [Bambuddy](https://github.com/maziggy/bambuddy) documentation.
