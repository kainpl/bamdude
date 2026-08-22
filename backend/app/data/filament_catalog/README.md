# Filament identity catalog (system tier)

`bambu.json` and `orca.json` are **generated** distillates of the BBL vendor's
filament profiles from the two slicer reference checkouts. They are the SYSTEM
tier of the filament family catalog (spec:
`docs/superpowers/specs/2026-08-22-filament-family-catalog-design.md`):
identity only — family (`filament_id` ↔ alias ↔ vendor ↔ type) and preset
(name ↔ versioned `setting_id` ↔ family ↔ temps ↔ compatible printers). Preset
*content* for slicing is deliberately NOT here; it stays with the slicer
sidecar / clouds / local presets.

Loaded by `backend/app/utils/filament_catalog.py`. The user's DB never holds
these rows — the user tier lives in `user_filament_presets` /
`user_filament_families` (m149).

## Regenerating (part of the BS / Orca Reference Sync protocol)

```bash
python scripts/distill_filament_catalog.py temp/references/BambuStudio   # -> bambu.json
python scripts/distill_filament_catalog.py temp/references/OrcaSlicer   # -> orca.json
```

Then `git diff` this folder — output is deterministic (sorted rows), so the
diff shows exactly what upstream changed. **Never hand-edit the JSONs.**

Current sources:

| File | Source | Tag |
|---|---|---|
| `bambu.json` | BambuStudio | v02.08.02.61 |
| `orca.json` | OrcaSlicer | v2.4.2 |

## Reading the distiller's error report

Lines like `unresolvable leaf (filament_id=None ...)` name presets whose
inherits chain never reaches a `filament_id`. In OrcaSlicer v2.4.2 this is a
**genuine upstream gap** for several community families (Overture, COEX —
their `<family> @base` profile does not exist anywhere in the tree). Such
presets cannot be assigned to an AMS tray by id, so excluding them from the
identity catalog is correct behaviour, not a distiller failure. BambuStudio
distills with 0 errors, and its preset count (1928) matches the live Bambu
cloud public listing exactly (verified 2026-08-22).
