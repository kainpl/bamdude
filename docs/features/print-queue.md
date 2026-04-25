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
- **Batch quantity** -- print multiple copies at once (every copy lives in the queue, no special "primary" copy)
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

### `created_by_id` audit

Adding to queue records *who* added the item. The Telegram bot, library bulk-add, per-printer "Print" button, and File Manager prints all propagate the acting user. Visible per row on the archive that the queue item produces. The VP auto-queue and webhook trigger paths legitimately leave it `NULL` (no authenticated user to attribute).

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

## :material-counter: Batch quantity > 1 — single source of truth

When you set quantity to **N**, **all N copies** are added to the queue at once. They share a `batch_id` (a UUID stamped on every copy) so you can still answer "how many of this batch finished?" after the live queue rows clean up.

- You can reorder, edit AMS, or cancel each copy individually before it starts.
- The very first copy doesn't get "direct dispatched" any more — every copy goes through the same queue path. This eliminates the historical "first archive lands ahead of N-1 copies still in queue" inconsistency.
- The endpoint response status is `"queued"` for the whole N-copy submission; `dispatch_job_id` and `dispatch_position` are nullable in this path.

`quantity == 1` direct dispatch (Print Now from a single archive) keeps the legacy behaviour — one queue item, one immediate dispatch.

---

## :material-database-arrow-right: Dispatch behaviour: one job at a time

When you queue prints to multiple printers, BamDude dispatches them **one at a time across the whole farm** — not in parallel per printer. The second printer's start is delayed by the first dispatch's FTP upload + MQTT `start_print` round-trip (typically a few seconds; occasionally tens of seconds for very large 3MFs).

!!! info "Why serialised?"
    Two dispatches racing on `INSERT INTO print_archives` would hit SQLite's single-writer semantics and the second one could fail mid-FTP with `database is locked` after the busy timeout. Serialisation trades a few seconds of latency on the second printer for a guarantee that no dispatch dies after the upload phase. PostgreSQL deployments inherit the same behaviour for symmetry.

The active-job toast in the bottom-right shows which job is dispatching and which jobs are queued behind it. The progress bar tracks the FTP upload phase; once a print is *running* on a printer, the dispatcher moves on to the next job — you don't wait for prints to **finish**, only for the dispatch step (upload + start command) to finish.

This is **dispatch** serialisation only. With three idle printers and three queue items you'll have all three printers running within a few tens of seconds — they just don't all start in the exact same instant.

---

## :material-history: Queue history & archives

In 0.4.0 the live queue and the durable history were split apart (migration `m019`).

- The **live queue** only shows unfinished items: `pending`, `printing`, `paused`, `waiting_*`, plus failed / cancelled / skipped rows kept around so the "Issues" section retry/unskip/remove UI keeps working.
- Completed queue items **auto-delete** once their archive lands. `on_print_complete` removes the queue row after the corresponding archive transitions to `completed`.
- Past queue items live on as **archives** — every archive row carries `queue_id` (which queue dispatched it) and optional `batch_id` (which N-of-M batch it belongs to). External / direct-dispatch / Print-Now archives fall back to the printer's default queue id so they're attributable too.

The queue counters in the printer queue header (Total / Pending / Printing / Completed / Failed / Cancelled) are **recomputed from `print_archives` on every read**, not stored on the queue. They stay consistent even when archives are renamed or moved between projects, and they don't drift when the queue auto-cleans.

To see archived queue items, open the **Archives** page and filter by printer. Failed dispatches show the verbose `error_message` on hover (short cause codes continue to live in the existing `failure_reason` field).

!!! tip "Dispatch-time archive starts as `printing`"
    Library-file dispatches now create the archive row directly in `status='printing'` — no transient "Archived" badge flash during the FTP+MQTT window. If dispatch fails after the row commits (FTP error, start-print error), a fresh-session helper flips the archive to `failed` / `cancelled` with the verbose `error_message` set, so a zombie `'printing'` row never sits stuck in the UI.

---

## :material-link-variant-off: Library file deletion — what happens to queue items

The `print_queue.library_file_id` foreign key is `ON DELETE SET NULL` (migration `m018`). On top of that, the `DELETE /library/files/{id}` endpoint applies extra in-app logic so SQLite installs (where `PRAGMA foreign_keys` is off by default) get the same behaviour as PostgreSQL:

| Queue item references the file | Result |
|---|---|
| Currently `status='printing'` | API returns **409 `file_in_use`** with `queue_item_ids[]`. Cancel or finish those prints first, then retry the delete. |
| Anything else (`pending`, `paused`, `waiting_*`, etc.) | BamDude **cascade-deletes** the queue items along with the library file. |

Archives keep their separate 3MF copy (the dispatch flow copies the bytes into the archive directory at print start) and survive — `print_archives.library_file_id` is set to NULL on delete instead of cascading.

`POST /library/bulk-delete` applies the same logic per file: blocked-by-printing files are reported under `skipped_files` instead of failing the whole batch.

!!! note "Pre-0.4.0 behaviour was different"
    Earlier versions used a SET NULL FK without the in-app cascade — deleting a library file left orphan queue items pointing at nothing, which the queue couldn't dispatch. Those rows had to be manually cleaned. m018 + the in-app cascade close that hole.

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

## :material-shield-check: H2D false-reprint guard

H2D Pro firmware (01.01.00.00 series) keeps `gcode_state=FINISH` for 48–55 seconds after accepting a new file before transitioning to `PREPARE`. The scheduler watchdog used to revert queue items to `pending` at 45 s if the state hadn't moved — and the next scheduler tick re-dispatched the job as a "reprint" the printer was already physically running.

The dispatcher now waits up to **90 s** for `subtask_id` to advance past the pre-dispatch value (the printer echoes the `submission_id` BamDude minted in its next `push_status` — that signal lands long before `gcode_state` does on slow firmware) before failing the dispatch. The watchdog also short-circuits as soon as the new `subtask_id` shows up, regardless of whether `gcode_state` has caught up.

You won't see "queue stuck" reports from this any more, including immediately after a print completes on H2D / H2C / H2S models.

---

## :material-lightbulb: Tips

!!! tip "Overnight Prints"
    Schedule longer prints to start overnight -- wake up to finished prints.

!!! tip "Smart Plug Combo"
    Combine scheduling with auto power-off for hands-free operation.

> Originally based on [Bambuddy](https://github.com/maziggy/bambuddy) documentation.
