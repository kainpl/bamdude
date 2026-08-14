<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="static/img/bamdude_logo_dark.png">
    <source media="(prefers-color-scheme: light)" srcset="static/img/bamdude_logo_light.png">
    <img src="static/img/bamdude_logo_dark.png" alt="BamDude Logo" width="300">
  </picture>
</p>

<h1 align="center">BamDude</h1>

<p align="center">
  <strong>Self-hosted print archive, management and automation system for Bambu Lab 3D printers</strong>
  <br>
  <em>Hard fork of <a href="https://github.com/maziggy/bambuddy">Bambuddy</a> by maziggy, with Telegram bot, multi-chat auth, Ukrainian locale and more</em>
</p>

<p align="center">
  <a href="https://bamdude.top/"><img alt="Website" src="https://img.shields.io/badge/Website-bamdude.top-2dd4bf?style=flat-square&logoColor=white"></a>
  <a href="https://bamdude.top/features/"><img alt="Features" src="https://img.shields.io/badge/Features-overview-10b981?style=flat-square&logoColor=white"></a>
  <a href="https://docs.bamdude.top/"><img alt="Documentation" src="https://img.shields.io/badge/Docs-docs.bamdude.top-3b82f6?style=flat-square&logo=readthedocs&logoColor=white"></a>
  <a href="https://t.me/+3KQl2uNtOwo3NTgy"><img alt="Telegram Community" src="https://img.shields.io/badge/Telegram-BamDude%20Friends-26A5E4?style=flat-square&logo=telegram&logoColor=white"></a>
  <a href="https://hub.docker.com/r/kainpl/bamdude"><img alt="Docker Hub" src="https://img.shields.io/badge/Docker-Hub-2496ED?style=flat-square&logo=docker&logoColor=white"></a>
  <a href="https://github.com/kainpl/bamdude/releases"><img alt="Latest Release" src="https://img.shields.io/github/v/release/kainpl/bamdude?style=flat-square&logo=github"></a>
  <a href="https://send.monobank.ua/jar/2vREyf3SrF"><img alt="Support BamDude" src="https://img.shields.io/badge/Support-monobank%20jar-ffd60a?style=flat-square&logo=buymeacoffee&logoColor=black"></a>
  <a href="https://app.drukarmy.org.ua/inv/ujnv7w8i"><img alt="Join DrukArmy" src="https://img.shields.io/badge/%F0%9F%87%BA%F0%9F%87%A6-Join%20DrukArmy-005bbb?style=flat-square"></a>
</p>

---

## Where this came from

BamDude grew out of a volunteer workshop. Its author volunteers with
[**DrukArmy**](https://drukarmy.org.ua/ua/about-us) — Ukraine's largest volunteer 3D-printing
effort for the front line — printing, and running the FPV direction as senior curator.

Batches, deadlines and a farm that has to keep moving around the clock do not fit any
off-the-shelf tool, so every feature here earned its place on a real order first. That is also
why Ukrainian is a first-class locale rather than an afterthought.

If you have a printer and want it to do something useful:
**[join DrukArmy](https://app.drukarmy.org.ua/inv/ujnv7w8i)**.

---

## Support

BamDude is free and stays free — AGPL-3.0, no paid tiers, no pro edition. The most valuable
support is a bug report, a translation PR or a star. If you would rather chip in:

| | |
|---|---|
| **Monobank jar** | https://send.monobank.ua/jar/2vREyf3SrF |
| **PayPal** | `pushkar.valeriy@gmail.com` |
| **USDT (TRC20)** | `TWe1MaXz7mpDZZqDkY7Az7NdZ6s9H5fvMF` |

---

## What's different from Bambuddy?

BamDude is a hard fork of [Bambuddy](https://github.com/maziggy/bambuddy), aimed at print farm operators. It still tracks upstream — each release is adapted through a tracked audit rather than a blind merge — so what the two projects share is deliberately **not** listed here. Each item below is either absent upstream or built on a different principle.

### A queue built for a farm, in two tiers

- **One queue per printer, plus an Auto-Queue that distributes between them.** Work you have already assigned waits in that printer's own queue. Work you have not goes to the Auto-Queue, which routes it to whichever printer can take it — matching filament type and colour, and preferring an idle printer without refusing a busy one.
- **A single dispatch layer.** Queued prints, prints started from the printer's own screen, and files sent straight from a slicer all leave through one dispatcher. It claims the printer for the whole plate change, creates exactly one archive per physical print, and runs the swap macro before letting the next job in.

### Firmware for the whole farm, not one printer at a time

- **A console for bulk firmware updates.** Upstream updates a printer. BamDude takes a set of them, groups the set by model, downloads each model's firmware **once** into a shared store, and fans the transfer out under a concurrency cap — applying remotely where the model allows it.
- **A printer that is printing is skipped, not interrupted**, and one printer failing does not stop the rest of the batch.
- It is deliberately a separate orchestrator from print dispatch: firmware is not a print, and the single-dispatch rule stays intact.

### Telegram as a full second interface

Upstream can send a Telegram message — a notification channel, one way, over the Bot API. BamDude's is a complete aiogram 3.x bot you can hold a conversation with:

- Live status, printer control, camera snapshots and print-speed mode
- **Print from the library** — pick a file, pick a model-compatible printer, print now or queue it
- **Queue management** — paginated list, detail, reorder, cancel
- **Maintenance** — see what is overdue, mark it done, edit the hours
- **Add a printer** — type an IP and let SSDP fill in the serial, name and model
- **Multi-chat with roles** — every chat gets its own permission group, so a shop-floor chat and an admin chat are not the same thing
- **Per-chat notification settings** — event types, quiet hours and the daily digest belong to the chat, not to a global switch
- **Actionable notifications** — "Clear plate" and "Mark maintenance done" are buttons in the message itself

### Zigbee, with no hub in between

- **BamDude drives the radio itself.** Upstream reaches Zigbee devices only *through* a Zigbee2MQTT bridge, as MQTT topics; BamDude talks to the dongle over USB or Ethernet, so smart plugs and temperature/humidity sensors pair into a network it owns — no Home Assistant, no Zigbee2MQTT, no broker to keep alive.
- **Reporting intervals per device**, defaulted from ZHA's own values and changed from the device's card.
- **Measurement history** — power draw and room conditions recorded as they arrive and kept for a month, charted per plug and per sensor. It is written above the plug drivers, so all five plug types get it rather than only the Zigbee ones.
- **Sensor alarms** — a lowest and/or highest value for anything a sensor measures, including its own battery.

### A Virtual Printer that saves into the library

- **A file sent from the slicer lands in the file library, not in the print archive.** Upstream's virtual printer archives whatever it receives, or holds it for review. BamDude's default mode saves the 3MF into the library as a file you can browse, tag, move into a folder and print later — which leaves the archive as what it is meant to be, a record of prints that actually happened.
- **It can also feed either queue tier** — a printer's own queue, or the Auto-Queue — as well as relay to a real printer.

### The archive is a record of prints, and it is kept honest

- **One record, not two.** Upstream keeps a print log beside the archive. BamDude has no separate log because the archive *is* the log: every print is a row there — with its file, its plate, its filament, its energy and how it ended — so there is never a second list to reconcile against the first.
- **A print nobody queued is still archived.** Started from the printer's own screen, or sent straight from a slicer — BamDude notices it and builds the archive around it.
- **If the file could not be fetched at the time, BamDude keeps trying.** A printer that was busy, offline or mid-reconnect leaves an archive with no 3MF behind it; a retry service fills it in later from four separate triggers — a sweep at startup, the printer reconnecting, the print finishing, and a button.
- **A reconciliation sweep** compares what BamDude believes is printing against what the printers actually report, so a job that ended in a way nobody saw does not sit there forever claiming to be running.
- **Two hashes per archive, because the file that prints is not always the file you handed over.** BamDude patches the 3MF on its way to the printer, so an archive records both the original and the bytes actually sent. Deduplication on disk keys off the original — the same plate printed on five printers is stored once — and deleting an archive removes the file only when the last reference to it goes.

### Projects that plan the work, not just group it

- **A print plan** — per-file copies with live filament, time and cost totals and per-row printed/remaining counters, applied back to the project's own targets in one click.
- **Defective parts count against the target.** A project that needs forty usable parts is not finished because forty came off the plates. Scrap is recorded per print and subtracted.
- **A file or folder can belong to several projects at once** — many-to-many, with per-chip unlink rather than one owner per file.

### Locations that nest

- A workshop holds shelves, a shelf holds printers. Printers, sensors and spool storage attach at whichever level fits, and the printers, queues and maintenance views group by them.

### A wider command base to the printer

Both projects can set a temperature, a fan speed and jog the bed. BamDude speaks a considerably wider slice of the MQTT protocol beyond that, mirrored from BambuStudio's own sequences:

- **The extruder and the steppers.** Push and retract filament, and release the motors so the head can be pushed by hand. Both refuse a cold nozzle and a running print.
- **The air duct as a whole**, not just a fan speed — mode, filtration and every fan the machine has, with each fan's controllability resolved per mode, and the steps the printer actually accepts rather than a made-up 0/25/50/75/100.
- **The print options** Bambu Studio exposes and upstream never sends: air-print detection, auto-recovery, automatic filament switching, filament tangle, nozzle-blob detection, plate alignment, plate marking, plate type, air purification, sound, and saving remote prints to storage.
- **The AMS as a device, not just slots** — calibrate it, switch its firmware personality, reset its sequence, change its user settings. Backed by a **Printer Settings** and an **AMS Settings** dialog, each writing through MQTT and recording every applied change in an audit table.
- **Filament calibration over MQTT** — Pressure Advance and flow-rate runs started, tracked and read back, with K-profiles per nozzle and per extruder.
- **Timelapse storage** — how much room is left, and dropping the oldest recording to make room for the next one.
- **The printer's answer is read back.** Commands carry an acknowledgement listener, so a refusal is reported as a refusal instead of as success — and temperature requests are bounded by the machine's own limits rather than by a fixed table.

Also here:

- **G-code macros** — sequences you define, sent over MQTT, with plate-swap macros firing automatically between queued prints.
- **3MF patching on the way to the printer** — mesh-mode flags and G-code injection are applied to a copy, so the archived file on disk stays the unpatched original.

### The printer's built-in storage, not only the card

X2D, P2S and the H2 family keep files in built-in storage as well as on a card. Both projects talk to those printers over FTP, which only ever sees the card — so with no card inserted the file browser is empty and every print fails, on a machine that needs no card at all.

BamDude speaks the printer's own file channel alongside FTP, and the difference shows up as ordinary behaviour rather than as a feature:

- **The file browser has a SD card / Internal storage switch**, and everything it does works on both — listing, downloading, plate previews, importing into the library, deleting. A printer with no card opens on its internal storage instead of on an empty list. Printers that have only a card show no switch and behave exactly as before.
- **A print goes to internal storage when there is no card**, over the same dispatch as always. Whether a printer gets this is decided by what the printer reports about itself, not by a list of model names — and a card that is present but unreadable still stops the print rather than being routed around.
- **A print started from the printer's own screen gets its file into the archive**, pulled back off the machine even when the card that would normally hold it isn't there.
- **Timelapses are found on whichever medium recorded them.** On internal storage the printer says which print each recording belongs to, so it is matched to the right archive outright instead of being guessed at from timestamps.

The channel exists only on that newer generation, and BamDude decides by asking the printer rather than by model name — an open port proves nothing, since on A1 and P1 the same port belongs to the camera.

### Smaller, but still ours

- **Ukrainian.** Upstream ships twelve locales and Ukrainian is not among them. BamDude ships English and Ukrainian only, and both are strict: a key missing from either fails CI, and so does a placeholder that drifted between them.
- **Swap Mode** — driving an A1 / A1 Mini plate swapper, with Kit, STL and JobOx profiles, swap files detected automatically, and the swap macro fired between queued prints.
- **Low-stock forecast alerts** — the reorder forecast raises a notification rather than only colouring a panel nobody has open.
- **An audit row for every applied change** to printer settings, AMS settings and calibration — what was sent, when, and by whom.
- **Notes on library files**, and **print-dialog options remembered per user and per printer model**.

---

## Why BamDude?

- **Own your data** — All print history stored locally, no cloud dependency
- **Works offline** — Uses Developer Mode for direct printer control via local network
- **Full automation** — Schedule prints, auto power-off, get notified when done
- **Multi-printer support** — Manage your entire print farm from one interface

---

## Features

<table>
<tr>
<td width="50%" valign="top">

### Print Archive
- Automatic 3MF archiving with metadata
- **3MF download recovery** — when the printer's FTP fails during archive, recovery triggers fire on startup / printer reconnect / print-complete / manual button; per-archive lock prevents duplicate FTP sessions
- **Per-plate awareness** — multi-plate prints record which plate of the source 3MF was actually printed; thumbnail, print info, G-code preview, and 3D model all reflect that plate (m038 backfills historical archives)
- 3D model preview (Three.js) with build-volume wireframe matching the printer's bed
- Duplicate detection & full-text search (source-hash chain-of-custody for patched files)
- **Save a printed file back into the library** — pick a folder and the archive's 3MF is read in the way an upload is, arriving with its metadata, thumbnail, per-plate detail and badges filled in. Saving the same print twice keeps the one already there rather than making a second copy
- **Per-plate layer counts** — read from each plate's own G-code, because plates of one file routinely differ by hundreds of layers
- Photo attachments & failure analysis
- Timelapse editor (trim, speed, music)
- Re-print to any printer with AMS mapping
- Archive comparison, tag management

### Monitoring & Control
- **Printer calibration** — bed leveling, vibration, motor noise, nozzle offset, high-temp heatbed (model-aware, from UI and Telegram bot)
- Real-time printer status via WebSocket
- Live camera streaming & snapshots — **fan-out broadcaster** so multiple browser tabs / HA cards / Frigate share a single upstream connection (the printer itself only allows one)
- **Camera Wall** — one live grid of every printer's camera; on-screen tiles stream live (configurable cap, default 4), off-screen tiles fall back to periodic snapshots, with per-tile offline / status / HMS-error overlays
- Streaming overlay for OBS
- External camera support (MJPEG, RTSP, USB)
- Build plate empty detection
- Printer control (stop, pause, resume, light, speed)
- **Printer storage browser on both media** — browse, download, preview plates, import into the library and delete on the SD card *and* on the built-in storage of printers that have it (X2D, P2S, H2C, H2D, H2D Pro, H2S), with a switch between the two and separate model / timelapse catalogues on the internal side
- **Timelapse pre-flight** — the tick is checked against the printer before it is promised: a machine with no SD card, an unreadable one or a read-only one is named in the print dialog with which of the three it is, and a queue whose printer has run out of room **pauses** instead of quietly dropping the recording. Printers with internal storage or a timelapse kit skip the card question entirely
- **Motion control** — a round Bambu-Studio-style pad for the toolhead (10 mm / 1 mm rings, Home in the middle), the nozzle-bed gap, manual extrude / retract with the extruder drawn from Studio's own artwork, and release-motors. Reads which axes the printer says are homed, refuses a cold extruder below 170 °C, and speaks the newer `xyz_ctrl` protocol where the machine offers it
- **Temperature control** — set nozzle, bed and chamber (both nozzles on dual-nozzle machines), with the limits the printer itself reports: an X1 on 220 V mains accepts a *lower* bed temperature, and the chamber ceiling differs per model. Zero always means off
- **Fan and air-duct control on every model** — per-fan speeds, air-duct mode and filtration, with each fan labelled the way your printer names it. Machines that describe no air duct (P1S / P1P / X1C / A1) get the same controls built from what they do report
- **Pause-state visualisation** — yellow status pip in compact mode, inline `Paused • {reason} · 14m` chip with live elapsed counter in card header (compact + expanded), instant WebSocket toast on RUNNING↔PAUSE edges with classified reason (door / filament runout / AI defect / plate-detect / etc.)
- AMS management (RFID re-read, slot config, **Filament Backup status**) and **auto-drying** — per-filament humidity thresholds + alarms, a badge showing the active filament + target temp, H2C support, and opt-in **Continue drying while printing** on capable firmware (H2D / H2C / H2S / P2S / X2D / A2L / X1C)
- HMS error monitoring with history — **actionable remediation buttons** (Resume / Stop / Ignore & Resume / Stop Drying / Turn off Fire Alarm / …) that send the command straight to the printer over MQTT
- **Heater temperature history** — per-printer nozzle / second-nozzle / bed / chamber charts with target overlays, current/avg/min/max stats, and 6h / 24h / 48h / 7d ranges (30-day retention, auto-pruned)
- Print success rates, filament usage, cost analytics

### Filament Calibration
- **In-app calibration wizard (Bambu Studio parity)** — runs the calibration tests from inside BamDude; no Bambu Studio desktop needed
- **Pressure Advance** — PA Line, PA Pattern, PA Tower; measured K saved to the printer's 16-slot K-profile history over MQTT and auto-bound to the AMS slot
- **Towers** — Temperature, Volumetric Speed, VFA and Retraction; print-and-eyeball with a finish-step result calculator
- **Flow Rate** — two-pass test (9-block coarse → 10-block fine, end-to-end auto-dispatch); pass-1 baseline auto-prefilled from your filament preset's `filament_flow_ratio`, override-able to a fresh 1.0 without editing the slicer profile
- Calibration scaffold sliced against your own printer / process / filament presets through the OrcaSlicer or Bambu Studio sidecar, then printed via the normal dispatch pipeline
- Per-`(printer, filament, nozzle, extruder)` result cache, H2D dual-extruder per-extruder tabs, Calibration History modal

### Scheduling & Automation
- Per-printer queues with status tracking (idle/printing/paused/error)
- **Queue organization** — group prints into collapsible batches, drag-reorder by grip handle, and sort the Printers page by ETA; the timeline shows only committed schedules
- **Per-printer Maintenance Mode** — park a printer out of service (drops out of dispatch, scheduler, auto-drying, and metrics, and disconnects MQTT) without deleting it
- **Archive a printer** — soft-retire a sold/decommissioned printer: it disappears from the Printers page, every picker, queues, dispatch, the scheduler, and MQTT, while its full print history is kept. Blocked while printing; cancels the printer's pending queue items. Restore or permanently delete it under Settings → Printing → Archived printers. Distinct from Maintenance Mode, which only parks a printer temporarily and keeps its card visible
- Auto error-pause on print failure (queue stops, user decides next step)
- Staggered start for farms (limit concurrent heating, bed temp monitoring)
- **Swap Mode** — A1 Mini / A1 plate swapper with multi-profile support (Kit, STL, JobOx), auto-detect swap files, per-job event selection (start sequence / change table), plate-clear auto-bypass
- **Swap macro auto-execution** — `swap_mode_start` before print, `swap_mode_change_table` after print, with ACK + stg_cur completion tracking, queue pause on failure
- **Quick Vibration Check toggle** — per-job toggle; when disabled, 3MF gcode post-processor comments out `M970` commands, recalculates MD5 sidecars, repacks archive
- **Auto-Print G-code Injection** — per-job toggle that splices operator-defined snippets into the plate gcode at `; MACHINE_START_GCODE_END` (start) / EOF (end), with `{placeholder}` substitution from 3MF header (incl. PrusaSlicer→Bambu aliases). Snippets stored as per-printer-model JSON in settings; folded into the same single 3MF open/repack cycle as Quick Vibration Check so multi-plate 50+ MB files aren't unzipped twice
- **G-code macros** — execute from printer menu, ACK-based MQTT confirmation, `stg_cur` completion tracking, real-time status on printer card
- **Bulk firmware updates** — a console that takes a whole set of printers, groups them by model, fetches each model's firmware once into a shared store and fans the transfer out under a concurrency cap; printers that are mid-print are skipped rather than interrupted, and one failure does not abort the batch
- Model-aware maintenance types with history tracking and Excel export
- Clear plate confirmation between prints
- Smart plug integration (Tasmota, HA, MQTT, REST/webhook, and **Zigbee** — BamDude drives the dongle itself, no Home Assistant or Zigbee2MQTT needed)
- **Zigbee environment sensors** — pair temperature / humidity sensors onto the same dongle, name them, place them, and set how often each one reports. Battery devices are deliberately never polled, since a sleeper answers on its own schedule
- **Sensor alarms** — a lowest and/or a highest value on anything a sensor measures, including its own battery. The two are independent rather than a range, so "never above 30" needs no invented floor
- **Measurement history** — power draw and room conditions recorded as they arrive and kept for a month, charted over 6 h / a day / two days / a week per plug and per sensor. Written above the plug drivers, so all five plug types are covered
- Energy consumption tracking, per print as well as over time
- **Nested locations** — a workshop holds shelves, a shelf holds printers; printers, sensors and spool storage attach at whichever level fits, and the printers, queues and maintenance pages group by them
- Auto power-on/off
- Background print dispatch with WebSocket progress
- **Slicer Pipelines** — save a slice setup (printer / process / per-slot filament presets + bed type) once, then slice-and-queue any library file or archive in one click; target a specific printer or a whole model class (fanned out across matching printers via the auto-queue distributor), with a pre-flight compatibility check, multi-copy fanout, a live per-copy runs dashboard, and retry-failed
- **Preheat & heat-soak** — optionally hold the bed (and, on chamber-equipped printers, the chamber) at temperature *before* a queued print starts, so engineering filaments get the adhesion/warp soak Bambu's firmware won't wait for; per-print chamber target worked out from the loaded AMS filament types (editable map), three hardware tiers handled automatically (active heater / sensor-wait / timer) with airduct-flap control, plus a per-print Inherit/On/Off override

### File Manager (Library)
- Upload and organize sliced files
- **Composite file tags + chip-row filter** — `format` / `readiness` / `modifiers` / `provenance` chips drive both the badge row and a chip filter on the toolbar (sliced vs project vs raw geometry, single- vs multi-plate, MakerWorld provenance)
- **User-authored tags** — cross-cutting labels with a tag-filter rail and a bulk Tag action (separate from the automatic format/provenance badges)
- Sort folders **by recent activity**, search recursively **inside subfolders**, and per-folder Markdown **description panels** (renders README.md)
- **All Files** now lists your own uploads; a new **External** sidebar entry holds linked-folder files
- **Per-plate gallery + 3D / G-code preview with build-volume wireframe** — multi-plate 3MFs expose every plate; library viewer hides tabs that don't apply to the file (e.g. no 3D tab for sliced `.gcode.3mf`); dual-handle layer slider (crop both top and bottom), travel-moves toggle, layer-play with 1× / 2× / 4× / 8× speeds, theme-synced canvas, wireframe / X-ray toggle, OBJ format support, Export-as-PNG
- External folder mounting (NAS, USB)
- STL / OBJ thumbnail generation — shaded surfaces with Lambertian lighting + transparent background so cards "float" on whatever theme is rendering them
- Folder structure with drag-and-drop
- Print directly or add to queue
- Duplicate detection
- **Trash bin with restore** — soft-delete with configurable retention (default 30 days), background sweeper hard-deletes past the window, opt-in scheduled auto-purge for old library files + archives; trash UIs render thumbnails and a unified split-button (trash + caret dropdown for purge-old)

### Projects
- Group related prints
- Track plates and parts
- **Print plan table**: per-file copies with live filament/time/cost totals + per-row printed/remaining counters
- **Headline "remaining" totals** on Print Jobs / Print Time / Filament Used cards (green when done, amber when there's work left)
- **One-click "Apply to project"** in print plan + BOM totals rows — writes plate count, parts count, and budget (filament + materials cost) into the project's target fields; project edit modal also pre-fills from the plan + shows a "From plan: N" hint to re-sync after changes
- Link folders or individual files from the File Manager — **many-to-many** (a file or folder can belong to several projects at once)
- Per-chip unlink (`×` on each project chip) for granular detach
- Import/Export as ZIP or JSON

</td>
<td width="50%" valign="top">

### Telegram Bot
- Full printer control: pause, resume, stop, light, speed, camera snapshot
- Printer calibration from bot: model-aware selection (bed leveling, vibration, motor noise, nozzle offset, high-temp heatbed)
- Printer status with model tag, maintenance indicators
- Edit printer hours, view/mark maintenance from bot
- Clear plate confirmation for queue auto-dispatch
- Print from Library: file → printer (model-filtered) → Print Now or Add to Queue
- Add to Queue: file → target (specific printer or model) → confirm
- Queue management: paginated list, detail, move up/down, cancel
- Add Printer: enter IP → SSDP auto-detect serial/name/model → access code → done
- Reply keyboard + inline menus + /start /status /camera /help commands
- Multi-chat auth with per-chat roles (BamDude permission groups)
- Per-chat notification events, quiet hours, daily digest
- Actionable notification buttons: clear plate, mark maintenance done, pause/stop on progress
- Auto-registration mode for new chats
- 13 handler modules, 171 i18n keys (EN/UK), MarkdownV2 formatting

### Notifications
- Telegram (auto-restart bot on config change), Discord, Email, Pushover, ntfy, CallMeBot, **Bark** (free iOS push, no account)
- Home Assistant, custom webhooks
- Customizable message templates (MarkdownV2 editor)
- Per-chat quiet hours & daily digest (Telegram)
- Actionable buttons: clear plate, mark maintenance done, pause/stop on progress
- Print finish photo, filament usage details
- HMS error alerts, bed cooled alerts
- Dedicated **AI Failure Detection** event (separate from hardware errors); finish photos embedded inline in completion / failure emails
- **Pause / resume events with classified reason** — `print_paused` carries normalised `{reason}` (door / filament runout / presence-check / file-pause-command / AI defect / plate-objects / user) + matched `{hms_code}`; `print_resumed` carries `{paused_for}` (mm:ss). Default ON for new providers, included in default Telegram-chat event set
- Queue events (waiting, skipped, failed)

### Spool Inventory
- Built-in inventory with AMS slot assignment
- Automatic filament consumption tracking
- Per-spool cost tracking
- Bulk spool addition
- **Mass actions on the Filament tab** — tick rows (or the whole page, or everything matching the filter) and Edit / Print labels / Reset usage / Archive / Restore / Delete in one go; works in both built-in and Spoolman modes
- Spool catalog, color catalog, low-stock alerts
- **Managed storage-locations catalog** — pick shelves/drawers/dryboxes from a managed list instead of free-text
- **Colour-aware reorder forecasting** — per-colour runway with material/brand filters and lead-time overrides. Spools you archived still count as **what you burned** while no longer counting as **what you have**, so retiring an empty spool does not collapse the rate onto its fresh replacement; a material you have run out of entirely stays on the panel for 90 days, because that is precisely the one to reorder
- **The manager remembers what you filtered to** — material, brand, colour, category, name, the archived tab, the usage and stock chips, the search box and the view all survive leaving the page, and "Clear filters" clears the memory too
- **CSV import / export** of the local inventory
- Opt-out toggle for auto-adding unknown RFID spools
- Spoolman integration

### Integrations
- **Server-side slicing** — OrcaSlicer + BambuStudio sidecar containers ship in the same Compose project (`--profile orca` / `--profile bambu` / `--profile all`); per-job slicer picker in the Slice modal with live reachability badges, bed-type override (Cool / Engineering / High-Temp / Textured PEI / SuperTack), inline multi-plate selection, owner-filter on preset dropdowns
- Spoolman filament sync
- MQTT publishing for Home Assistant
- Prometheus metrics for Grafana
- Local OrcaSlicer preset import
- **Orca Cloud profile sync** — sign in and use your Orca Cloud printer/process/filament presets as a preset tier in the slice dialog and AMS-slot picker
- K-profiles (pressure advance)
- Git backup (GitHub + GitLab)
- API keys & webhooks
- LDAP/Active Directory authentication

### Virtual Printer & Remote Printing
- Proxy Mode for remote printing via TLS relay
- Four modes: **File Manager** (the default), **Printer Queue**, **Auto-Queue**, and **Proxy**
- **File Manager mode** — saves received 3MF files into the library rather than archiving them, so the archive stays a record of prints that actually ran
- **Queue modes** — hand a received file straight to one printer's queue, or to the Auto-Queue to be routed across the farm
- SSDP discovery or manual IP
- **Per-VP G-code injection toggle** for auto-eject / plate-clear rigs; Bambu Studio "Send all plates" queues one item per plate
- **Tailscale reach** — when the host runs Tailscale, the VP card shows the tailnet IP and MagicDNS name to paste into the slicer. The CLI ships in the Docker image; mount `/var/run/tailscale/tailscaled.sock` (see `docker-compose.yml`) to enable it. Trust still goes through the one-time `bbl_ca.crt` import — slicers validate MQTT only against the bundled BBL CA store, so a publicly-signed cert can't replace it

### Authentication
- Group-based permissions (80+ granular)
- JWT tokens, API key support
- **OIDC (OpenID Connect)** — PocketID, Authentik, Keycloak, Authelia, Google, **Azure Entra ID** (`preferred_username` / `upn` claim, optional skip of `email_verified` gate)
- **SSO autologin** and an option to **disable local username/password login** (lockout-guarded)
- **Admin session-lifetime ceiling** (Settings → Users → Session Policy, up to 30 days)
- LDAP/Active Directory with group mapping
- Per-user Bambu Cloud accounts
- Advanced Auth via Email (SMTP)
- Per-user email notifications
- **Long-lived camera-stream tokens** — per-user, named, revocable tokens with hard 365-day TTL for Home Assistant cards, Frigate inputs, and wall-mounted kiosks

</td>
</tr>
</table>

**Plus:** Customizable themes, mobile responsive, multi-language (EN/UK), auto updates, database backup/restore, PostgreSQL support

---

## Quick Start

### Requirements
- Python 3.12+ (only for a native install — the Docker image bundles its own)
- Bambu Lab printer with **Developer Mode** enabled
- Same local network as printer

### Docker Hub (Recommended)

```bash
docker run -d \
  --name bamdude \
  --network host \
  -e TZ=Europe/Kyiv \
  -v bamdude_data:/app/data \
  -v bamdude_logs:/app/logs \
  --restart unless-stopped \
  kainpl/bamdude:latest
```

Open **http://localhost:8000** in your browser.

> **macOS/Windows:** Docker Desktop doesn't support `--network host`. Use `-p 8000:8000` instead and add printers manually by IP.

### Docker Compose (from source)

```bash
git clone https://github.com/kainpl/bamdude.git
cd bamdude
docker compose up -d --build
```

### Manual Installation

```bash
git clone https://github.com/kainpl/bamdude.git
cd bamdude
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --loop asyncio
```

**Windows (native):** download the latest `bamdude-windows-setup.exe` from the [Releases page](https://github.com/kainpl/bamdude/releases) and run it. It's a self-contained installer (embedded Python — no Python or Node install required) that sets up a data directory and registers BamDude as an NSSM-supervised Windows Service that autostarts on boot.

See [`install/README.md`](install/README.md#windows-installer-exe-windows-1011) for options, or [`installers/windows/`](installers/windows/) to build the installer yourself.

### Upgrading or migrating

Full manual: **<https://docs.bamdude.top/getting-started/upgrading/>** ([source](https://github.com/kainpl/docs.bamdude.top)) — covers migration from Bambuddy 2.2.2, from Bambuddy-HE / BamDude 0.2.x, routine BamDude-to-BamDude updates, switching between self-install / Docker / GHCR, and rollback.

Short version:

- **From Bambuddy 2.2.2** (tested & supported) — drop `bambuddy.db` into BamDude's `data/` and start. The `m000` migration imports automatically and renames the file to `bamdude.db`.
- **From Bambuddy-HE / BamDude 0.2.x / 0.3.x** (tested & supported) — Docker users run `install/migrate-volumes.sh` once to copy `bambuddy_he_*` → `bamdude_*`; native users just point the installer at the existing data dir.
- **From Bambuddy 0.2.3 or newer** — ⚠️ not tested. BamDude diverged from upstream at 2.2.2 and applies its own migrations; newer upstream schemas may hit `no such column` errors on boot. Back up first, keep the Bambuddy data directory untouched, and file an issue if you hit a wall.

### Telegram Bot Setup

1. Create a bot via [@BotFather](https://t.me/BotFather), get the token
2. In BamDude Settings > Notifications, add a Telegram provider with the bot token
3. Enable Registration Mode, send `/start` to the bot from your Telegram
4. In Settings > Telegram Chats, assign a role to your chat and enable it
5. Done! Use the reply keyboard or inline menus to control printers

### Enabling Developer Mode

1. On printer: **Settings** > **Network** > **LAN Only Mode** > Enable
2. Enable **Developer Mode**
3. Note the **Access Code** and **IP address**

---

## Tech Stack

| Component | Technology |
|-----------|------------|
| Backend | Python, FastAPI, SQLAlchemy, aiogram 3.x |
| Frontend | React 19, TypeScript, Tailwind CSS 4 |
| Database | SQLite (default) or PostgreSQL |
| 3D Viewer | Three.js |
| Communication | MQTT (TLS), FTPS |
| Telegram | aiogram 3.x, MarkdownV2, FSM |

---

## Supported Printers

| Series | Models |
|--------|--------|
| X1 | X1, X1 Carbon, X1E |
| H2 | H2D, H2D Pro, H2C, H2S |
| P1 | P1P, P1S |
| P2 | P2S |
| X2 | X2D |
| A2 | A2L |
| A1 | A1, A1 Mini |

---

## Development

```bash
# Backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
# --loop asyncio is not optional, in development either: uvicorn[standard]
# picks uvloop, whose TLS layer can silently truncate a file transfer.
DEBUG=true uvicorn backend.app.main:app --reload --loop asyncio

# Frontend (separate terminal)
cd frontend && npm install && npm run dev
```

---

## License

AGPL-3.0 License — see [LICENSE](LICENSE) for details.

---

## Acknowledgments

- [Bambuddy](https://github.com/maziggy/bambuddy) by maziggy — the original project this is forked from
- [Bambu Lab](https://bambulab.com/) for amazing printers
- The reverse engineering community for protocol documentation
