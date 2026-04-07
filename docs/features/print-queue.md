---
title: Per-Printer Queues
description: Independent print queues per printer with scheduling and automation
---

# Per-Printer Queues

Queue and schedule prints with independent per-printer queues, drag-and-drop ordering, batch quantity, and smart automation.

---

## :material-playlist-plus: Overview

The print queue lets you:

- **Queue prints** from archives or the file manager
- **Per-printer queues** -- each printer has its own independent queue
- **Batch quantity** -- print multiple copies at once
- **Drag-and-drop** ordering
- **Scheduled** start times
- **Timeline view** -- production schedule with estimated completion times
- **Model-based assignment** -- queue to "any printer of matching model"
- **Smart plug automation** -- auto power-on/off

---

## :material-plus: Adding to Queue

### From Archive

1. Go to **Archives** page
2. Click the **Schedule** button on the archive card
3. Choose target printer(s)
4. Optionally configure filament mapping
5. Print is added to queue

### From File Manager

1. Select sliced files in **File Manager**
2. Click **Add to Queue** in toolbar
3. Choose target printer

### AMS Filament Mapping

When adding multi-color prints, configure which AMS slot to use for each filament. Auto-matching by type and color is available, with manual override.

!!! tip "Stored Mappings"
    AMS mappings are saved with the queued print. When it starts, BamDude uses your configured mapping.

---

## :material-sort-ascending: Shortest Job First (SJF)

Prioritize shorter print jobs for faster throughput.

1. Click the **SJF** badge in the queue header
2. Shortest pending prints are dispatched first
3. Starvation guard ensures long jobs still get printed

---

## :material-drag: Drag and Drop Ordering

1. Hover over a queued print
2. Grab the drag handle
3. Drag to new position
4. Prints execute top to bottom

---

## :material-clock-outline: Scheduling

- **Immediate** -- starts when printer is idle
- **Scheduled** -- starts at a specific date/time
- **Queue Only** (staged) -- won't start automatically until manually released

---

## :material-cancel: Managing Queue

### Clear Plate Confirmation

After a print finishes, the next print does **not** start automatically. A **"Clear Plate & Start Next"** button appears on the printer card.

Disable this in **Settings > Queue > Require plate-clear confirmation** for automated workflows.

### Bulk Editing

Select multiple queue items to reassign printers, toggle options, or cancel in bulk.

---

## :material-printer: Multi-Printer Selection

Send the same print to multiple printers at once:

1. Open **Add to Queue** modal
2. Select multiple printers using checkboxes
3. Configure per-printer AMS mapping if needed
4. Submit to all

---

## :material-bell-ring: Queue Notifications

| Event | Description |
|-------|-------------|
| **Job Waiting** | Job waiting for filament |
| **Job Skipped** | Job skipped due to previous failure |
| **Job Failed** | Job failed to start |
| **Queue Complete** | All queued jobs finished |

Configure in **Settings > Notifications**.

---

## :material-lightbulb: Tips

!!! tip "Overnight Prints"
    Schedule longer prints to start overnight -- wake up to finished prints.

!!! tip "Smart Plug Combo"
    Combine scheduling with auto power-off for hands-free operation.

> Originally based on [Bambuddy](https://github.com/maziggy/bambuddy) documentation.
