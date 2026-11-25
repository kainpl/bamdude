# Invariants

The rules a change must not break, in full: what each one requires, why, the
traps it closes and the alternatives that were rejected. [`CLAUDE.md`](../../../CLAUDE.md)
keeps every rule in short form under the same heading and links each area here;
read an area's file before changing code its rules name.

| Area | Covers |
|---|---|
| [platform](platform.md) | async I/O and MQTT clients, the collector, request transactions, null updates, API refusal translation, case folding, i18n, child-process logging, security headers |
| [auth](auth.md) | always-on auth, credential lifetimes, 2FA/OIDC, rate limits, permissions, API-key printer scope, camera and media tokens, token-in-URL routes, integration credentials, live delivery |
| [mcp](mcp.md) | the MCP endpoint: request-local authority, current job, period totals, diagnostic history, HTTP setup |
| [database-and-restore](database-and-restore.md) | startup order and the SQLite → PostgreSQL import, import repair, portable backup and restore, backup files, Git restore and export, content deletion, restore-owned uploads and Virtual Printers |
| [printers](printers.md) | live layers and the observed run, command acknowledgements, the crash-start pause, Stop, macros, deleted and archived printers, jog, H2C rack, preheat, drying, Virtual Printer, next pause, Obico |
| [archive](archive.md) | the archive row and its 3MF, adoption, download recovery, folders, chain of custody, the patcher, plate selection, print doors, editable fields, wear cost, the "not checked" mark |
| [queue](queue.md) | the dispatch layer and claim, lanes, the previous-print gate, batches, routing, rebalancing, reconciliation, the dispatch guard, stagger groups, captured sources, AutoQueue transfer and returns, operator recovery, swap facts, the slicer connector |
| [workshop](workshop.md) | rights, server-side lists, entity codes, the order journal, product facets, line composition and configuration, the product gate, finished goods, stock picking, fulfilment, the parts ledger, defects, the working plan, part pictures |
| [filament-and-slicing](filament-and-slicing.md) | filament attribution, Spoolman tracking, advertised AMS profiles, slot matching, spools in slots, weight sync, refills, K-profiles, spool names, colour names, bulk inventory, the slicing setup, preset identity |
| [energy-and-plugs](energy-and-plugs.md) | the plug switch seam, per-print energy, the price history, stalled meters, Zigbee plugs and sensors, measurement history |
| [media](media.md) | download jobs, printer file deletion, archive videos, the video editor, layer timelapse frames, finish photos |
| [rendering](rendering.md) | part render process trees, the render bundle, the Node pin, result directories, plate metadata, STEP thumbnails and their copyleft sources, plate thumbnails, labels |
| [library-and-model-sources](library-and-model-sources.md) | folder scans, file visibility, file photos, model providers, the pinned transport for self-hosted sources |
| [notifications](notifications.md) | Telegram chats and bots, the in-app inbox |
| [frontend](frontend.md) | modals, page scrolling and sticky bars, the Safari 16 baseline |

`Vault:` pointers name notes in the maintainer's private knowledge base; the
text here does not depend on them.

A new rule gets its short entry in `CLAUDE.md` and its full text here in the
same change; a changed rule changes both.
