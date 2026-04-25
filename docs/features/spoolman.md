---
title: Filament Inventory & Spoolman
description: Built-in BamDude spool inventory plus optional bi-directional sync with a self-hosted Spoolman instance
---

# Filament Inventory & Spoolman

BamDude ships with a **first-class filament inventory** (Settings → Filaments). It is the source of truth for spool weight, RFID, location, and cost — no external service required. If you already run [Spoolman](https://github.com/Donkie/Spoolman), the optional sync layer keeps both systems in step; if you don't, BamDude does the whole job on its own.

!!! tip "You don't need Spoolman"
    Built-in inventory works fully standalone. Spoolman sync is purely an integration choice for users who already centralise spools across multiple tools (e.g. OctoPrint, Mainsail, Klipper, multiple slicer hosts). Pick whichever flow matches your setup.

=== ":material-package-variant: Built-in inventory"

    BamDude-native — no external service, no network round-trips, every column lives in `data/bamdude.db`.

=== ":material-sync: Spoolman sync"

    Add-on integration with a self-hosted Spoolman server. Bidirectional, tunable per concern (weight vs. location vs. partial-usage reporting).

---

## :material-package-variant: Built-in Inventory

Open **Settings → Filaments**. Each row is a physical spool — manually added, RFID-imported, or auto-created from an AMS scan.

### :material-plus-box: Adding spools

The "Add Spool" form covers everything BamDude tracks per spool:

| Field | Notes |
|-------|-------|
| `brand` | Free text (e.g. `Polymaker`, `Bambu Lab`, `SUNLU`). |
| `material` | `PLA`, `PETG`, `ABS`, `TPU`, `PA`, `PC`, `PVA`, `ASA`, … |
| `subtype` | `Basic`, `Matte`, `Silk`, `CF`, `Tough`, … |
| `color_name` + hex | Free-text colour name plus an `#RRGGBBAA` swatch. The hex input normalises every keystroke — paste `#FFAA00` and BamDude pads it to `FFAA00FF`. |
| `purchase_date` | When you actually bought it. Distinct from `created_at` (when the row was imported). The "Added" column prefers this when set. |
| `filament_diameter` | `1.75` or `2.85`. Defaulted to `1.75`. |
| `label_weight_g` | Advertised net weight on the label (default 1000 g). |
| `core_weight` | Empty-spool weight, used for scale-based remaining calculations. Looked up from the catalog when brand+spool match. |
| `lot` | 1-based position inside a purchase bundle. The bulk-add path can auto-number `1..N` server-side via the **auto-increment lots** checkbox. |
| `cost_per_kg` | Bare number, no currency symbol. Multiplied by per-print weight for the archive's cost field. |
| `note` | Free-form text (`Kitchen shelf`, `Open since Apr 12`, …). |
| `tag_uid` / `tray_uuid` | RFID identifiers. Empty for manually-added spools — bind a tag later via the matcher. |

Bulk-add takes a quantity and creates N rows in one go — combine with **auto-increment lots** to number a 5-spool bundle as `lot 1..5` without typing each one.

### :material-format-text: Spool display-name template

The Filaments page synthesises a human label per spool via a user-configurable template — search and sort use the same string. Edit it under **Settings → System → Spool Display Template**.

Default: `{brand} {material} {color_name}` (renders as e.g. `Polymaker PLA Jade White`).

| Token | Source | Example |
|-------|--------|---------|
| `{brand}` | column | `Polymaker` |
| `{material}` | column | `PLA` |
| `{subtype}` | column | `Matte` |
| `{color_name}` | column | `Jade White` |
| `{slicer_filament_name}` | column | `Polymaker PolyTerra PLA @Bambu Lab X1C` |
| `{note}` | column | `Kitchen shelf` |
| `{label_weight_g}` | column | `1000` |
| `{label_weight_kg}` | computed | `1` (round) or `0.75` (fractional) |
| `{remaining_g}` | computed `label − used` | `750` |
| `{remaining_kg}` | computed | `0.75` |
| `{remaining_pct}` | computed | `75%` |
| `{color_hex}` | computed from `rgba` | `#FF3300` |
| `{cost_per_kg}` | column | `25` |
| `{purchase_date}` | column | `2026-04-15` |
| `{filament_diameter}` | column | `1.75` |
| `{lot}` | column | `3` |

!!! tip "Unknown tokens stay verbatim"
    Typo a token like `{brnd}` and the live preview keeps it as-is — that surfaces the mistake immediately instead of silently collapsing to an empty space.

### :material-view-column: Column visibility

Click **Column Config** on the Filaments page to toggle which columns are visible and in what order. Settings are per-user.

**Visible by default:** `brand`, `material`, `color_name`, `remaining`, `location`, `note`, `purchase_date`.
**Hidden by default:** `created_at` ("added time" — superseded by `purchase_date`).

Newly-added columns land at their default position rather than being appended to the end, so post-upgrade existing users don't have to re-arrange.

### :material-magnify-scan: Auto-assign by RFID

The Filaments page header has an **Auto-assign** action: BamDude scans every connected printer's AMS slots, matches each slot's `tag_uid` / `tray_uuid` against inventory rows, and creates `SpoolAssignment` records in bulk. Useful after a multi-spool reload — one click, no manual picking.

### :material-link-plus: Bind unknown RFID to a manual spool

When an unknown RFID tag appears on a printer, the AMS slot popover offers to bind it to an existing inventory row that doesn't have a tag yet. Use case: third-party brands without RFID, refilled cores, or a spool you bought before you started using BamDude. Pick the row, confirm, and the tag is now attached — next scan auto-resolves.

---

## :material-sync: Spoolman Sync

Optional. Connect BamDude to a [Spoolman](https://github.com/Donkie/Spoolman) instance and the two systems mirror each other.

### :material-link: Connecting

1. **Settings** → **Integrations** → **Spoolman**
2. Set the **URL** (e.g. `http://192.168.1.50:7912` or a docker-compose service alias like `http://spoolman:7912`)
3. **Test Connection**
4. **Save**

!!! tip "Network reachability"
    BamDude must be able to reach the Spoolman URL from inside its own process. On docker-compose, put both services on the same network and use the service alias; on bare metal, a LAN hostname or static IP is enough.

### :material-tune: Sync controls

| Setting | Effect |
|---------|--------|
| `spoolman_enabled` | Master switch. |
| `spoolman_sync_mode` | `auto` (push every AMS change immediately) or `manual` (wait for an explicit Sync button click). |
| `spoolman_disable_weight_sync` | Skip `remaining_weight` updates on existing Spoolman spools — only push location. Use this when Spoolman is your authoritative weight tracker (its granular usage reporting beats AMS estimates). |
| `spoolman_report_partial_usage` | When a print fails or is cancelled, report the **estimated grams used up to the abort point** based on layer progress, instead of dropping the whole estimate. Helps Spoolman keep an accurate weight after failures. |

### :material-sync-circle: What syncs

- **AMS slot ↔ Spoolman spool** — Each loaded slot maps to a Spoolman spool ID. Material, brand, colour, and (unless `disable_weight_sync` is on) remaining weight are kept in step.
- **Print consumption** — Each completed print reports the grams used to Spoolman as a usage event. Cancelled / failed prints respect `spoolman_report_partial_usage`.
- **Location** — BamDude writes the printer name + AMS coordinates to Spoolman's `location` field (`H2D-1 AMS-A Slot 3` etc.). Always synced even with weight sync disabled.
- **RFID** — Bambu Lab tray UUIDs are passed through to Spoolman's tag field.

### :material-link-off: Unlinking

In `manual` sync mode, each Bambu spool card shows an **Unlink** button — useful when you want to migrate a spool from Spoolman back to BamDude-only inventory without breaking the AMS assignment.

---

> Originally based on [Bambuddy](https://github.com/maziggy/bambuddy) documentation.
