# AutoQueue filament routing

AutoQueue reads the selected plate from the sliced 3MF and checks every filament channel used by that plate. Printers of the same model can have different feed configurations. The form shows separate groups for AMS connected, no AMS, and unknown configuration, with compatible and ready counts shown separately.

## Choosing the rules

- **Automatic source** allows any complete supported combination of AMS and external feeds.
- **AMS only** requires AMS sources for every used channel.
- **External only** allows supported external feeds even when the printer has an AMS attached.
- **Require exact colors**, off by default, requires the file's colors. With it off, exact matches are preferred, while another color of the required material may be used. A color pinned on an individual channel remains required.
- **Allow match by base material**, on by default, uses the resolved filament family's `filament_type` such as `PETG`, rather than the sliced profile name, vendor, or product variant. A child profile inherits that field through its base preset: a custom 333Print PETG profile can therefore use Generic PETG. When the switch is off, the profile's own type is required and a known `tray_info_idx` variant remains a restriction. An explicit per-channel material override takes precedence over the family material.

The number of copies does not change these choices. Different channels always need distinct physical sources. Multicolor jobs remain supported: two channels cannot be assigned to a single external spool. Supported dual-nozzle printers can use separate external feeds or AMS on one nozzle and external on the other when the file and live printer configuration establish the correct nozzle bindings. This applies to the shared model capability registry, including models beyond X2D.

Material names use the shared case-insensitive compatibility table; at present only `PA-CF`, `PA12-CF`, and `PAHT-CF` are one interchangeable group. Exact colour preference remains ahead of “Drain the emptiest spool first”. That farm setting is on by default and ranks only otherwise-equivalent automatic sources: inventory/Spoolman tracked AMS grams first, then firmware-only remaining percentages, with unknown values last. It never overrides material, nozzle, source policy, a physical pin, or a strict-colour requirement, and is skipped when AMS Filament Backup is known to be off.

## Print profiles and macros

An AutoQueue dialog intentionally contains routing and scheduling choices only. It does not show print-option, event-macro, or Swap-macro controls: before a printer is selected, one set of values would be misleading on a mixed-model farm.

When the router promotes a job into a concrete printer queue, it resolves **Saved print profiles** for that printer's model and for the operator who created the AutoQueue item. This supplies calibration modes, recording and G-code options, and the enabled event macros for that model. If the operator has no profile, the system profile for that same model is used; if neither exists, ordinary queue defaults apply. A P1S-only light macro, for example, is selected when the job lands on a P1S, not when it was initially added to AutoQueue.

Swap macros are also read from that target model profile, but run only when that specific printer has Swap mode enabled and the source file does not already contain baked-in swap macros. A normal printer, a source with baked-in macros, or missing source metadata always suppresses them to avoid a double plate-change sequence.

## Files and waiting

A whole-file choice is accepted when the file contains one unambiguous printable plate; its actual plate number is retained. A multi-plate file needs an explicit selection. Missing G-code, incomplete filament usage, or missing nozzle bindings produce a source error before a new job is added. Project quantities and recipe whole-file selections are preserved; queued jobs receive the resolved printable plate.

A valid job can wait when no printer is currently compatible or ready. A temporary failure to obtain live compatibility information does not prevent adding a valid source. The preview is advisory and does not reserve a printer.

The server checks routing again before preparing a print and immediately before publishing its start command. A changed spool, connection, source file, or queue claim can defer the attempt. A queued job returns to waiting with a reason; a direct Print Now refusal does not schedule a future print. An aborted preparation is excluded from print and production counts, and its original source is retained.

The router searches for a complete mapping rather than taking the first usable slot: a flexible channel cannot consume the only source required by a pinned or strict-colour channel. Remaining filament is a ranking signal, not a reservation or a check that the reported grams cover the slicer's estimated demand.

## Editing, copying, and existing queues

When a job is accepted, BamDude takes one verified immutable copy of the actual bytes into `data/queue-sources/`. The queue then reads that copy for routing, preparation and upload. A laptop may sleep, an SMB share may disconnect, an external library file may move or be deleted, and archive retention may run without changing an already accepted job. The same bytes queued many times occupy one shared copy. It remains while at least one printer-queue or AutoQueue row owns it, then cleanup releases it. The portable backup includes every ready copy named by its database snapshot, so restoring on a different machine does not need the original folders. `staging/` contains only unfinished copies and is never a queued job or a backup payload.

**Copy Queue reuses a ready job's saved copy.** It does not reopen its archive, library record, NAS path or any other original. The target receives a second reference to the same verified bytes, while its printer selection, AMS mapping, schedule and print options are chosen afresh. A legacy row still follows its original-file workflow. A missing or broken saved source remains visible but cannot be copied; it never falls back to a potentially changed original.

Rows created before this change remain legacy rows until they are next captured. A snapshot that is missing or fails its checksum, or a legacy source that disappears, is marked **File error** and skipped. Other jobs continue; the printer queue is not paused and this does not count as a failed physical print. AutoQueue keeps the failed row visible, and a printer's queue shows it under Issues. Restore access only for a legacy row, then use **Retry**. A ready snapshot never retries an old NAS path.

Routing choices are stored with each printer-queue job. Removing its original AutoQueue row does not remove its rules. Editing only its schedule, cloning, retrying, and repeating preserve those choices. Explicit physical slot selections remain pinned; moving them to a different printer or plate requires a new mapping answer. File-specific color pins cause each following file in a grouped add to be shown for review.

Existing rows are migrated using their stored data only. Old automatic jobs that explicitly disabled AMS keep the external-only restriction. A physical AMS mapping with an inconsistent old `use_ams` flag remains physical intent. Missing or unrecognized policy evidence requires review instead of silently relaxing the rules.

Raw G-code and server-created calibration jobs keep their separate workflows. Full-slot and unsliced-file previews used for slicing also keep their existing contract.

Validation and the implementation acceptance map are in [the routing test matrix](testing/auto-queue-filament-routing.md). Tests use synthetic files and mocked device transport. Actual firmware behavior and the original farm incident require separate observation on hardware.
