# Bambu Lab printer configs (mirrored from BambuStudio)

**Byte-for-byte copies** of BambuStudio's `resources/printers/<code>.json`, one
per printer model. They are the data-driven source of truth for **per-model
device capabilities** — so this knowledge lives in data, not hardcoded Python.

- **Source:** BambuStudio `resources/printers/` @ tag **`v02.08.01.55`**
  (commit `ba049f6a`, 2026-07-14).
- **Consumed by:** `backend/app/utils/printer_configs.py` (loader + the
  device-calibration availability resolver).
- **Local BS checkout** (not in this repo): `temp/references/BambuStudio/`.

## What's inside each file

Keyed by **firmware version**; `"00.00.00.00"` is the base / default block. Its
`print` sub-object carries the capability flags we read:

| Field | Used for |
|-------|----------|
| `support_lidar_calibration` + `support_ai_monitoring` | Micro-lidar device calibration (X1 only) |
| `support_bed_leveling` (0/1/2) | Auto bed-leveling; `2` = off/auto/on tri-state |
| `support_motor_noise_cali` | Motor-noise cancellation |
| `support_nozzle_offset_calibration` | Nozzle-offset calibration (dual-nozzle) |
| `support_high_tempbed_calibration` | High-temp bed leveling |
| `support_clump_position_calibration` | Nozzle-clumping detection |
| `support_auto_flow_calibration`, `support_chamber*`, `ipcam`, … | (available for future data-driven gating) |

The files are named by **internal code** (`N6.json` = X2D). The loader resolves a
model — display name (`X2D`), long form (`Bambu Lab X2D`), or code (`N6`) — to a
file using each JSON's own `display_name` / `model_id`, so it is independent of
`PRINTER_MODEL_ID_MAP`.

### Code ↔ model (from the JSONs' own `display_name`)

`BL-P001`=X1C · `BL-P002`=X1 · `C11`=**P1P** · `C12`=**P1S** · `C13`=X1E ·
`N1`=A1 mini · `N2S`=A1 · `N6`=X2D · `N7`=P2S · `N9`=A2L · `O1C`/`O1C2`=H2C ·
`O1D`=H2D · `O1E`=H2D Pro · `O1S`=H2S.

> ⚠️ Note: `PRINTER_MODEL_ID_MAP` in `printer_models.py` has `C11`→X1C and
> `C12`→X1, which the BS configs contradict (`C11`=P1P, `C12`=P1S). That stale
> map is a separate pre-existing bug; this loader deliberately does **not** rely
> on it.

## Re-sync protocol (when BS ships new firmware/features)

1. Update the local BS checkout to the new tag (`temp/references/BambuStudio/`).
2. Re-copy: `cp temp/references/BambuStudio/resources/printers/*.json backend/app/data/printers/`.
3. `git diff` — **byte-identical unless BS actually changed a model.** Review any
   diff (new `support_*` flags, new models) and wire it up in `printer_configs.py`
   + the calibration UI as needed.
4. Bump the tag/commit noted above.

## License

These are verbatim files from BambuStudio (AGPL-3.0). We mirror them as
factual per-model configuration for interoperability; see the project's
`CONTRIBUTING` / attribution notes.
