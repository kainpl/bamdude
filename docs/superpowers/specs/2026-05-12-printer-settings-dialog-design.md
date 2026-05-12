# Printer Settings Dialog — Design

**Date:** 2026-05-12
**Status:** Draft, awaiting user review
**Scope:** Port Bambu Studio's three printer-side dialogs (Calibration, Print Options, Printer Parts) into a single BamDude tabbed modal opened from the printer-card "three dots" context menu. Per-printer, per-model gated, audited.

---

## 1. Goal

Surface all toggles Bambu Studio exposes under its printer-status panel — kick a full calibration run, manage ~20 print-time monitoring / behaviour toggles, view nozzle hardware — directly inside BamDude. No need to switch to BS just to flip a flag or start a calibration.

## 2. Authoritative source

All command shapes, gating rules, dialog copy, and confirm wording come **verbatim from Bambu Studio**:

- `temp/references/BambuStudio/src/slic3r/GUI/Calibration.hpp,.cpp` — calibration dialog
- `temp/references/BambuStudio/src/slic3r/GUI/PrintOptionsDialog.hpp,.cpp` — print options + printer parts dialogs
- `temp/references/BambuStudio/src/slic3r/GUI/DeviceManager.cpp:1738` — `command_set_printing_option(auto_recovery)`
- `temp/references/BambuStudio/src/slic3r/GUI/DeviceManager.cpp:1787` — `is_support_command_calibration()` (X1 rv1126 < 00.00.15.79 fallback)
- `temp/references/BambuStudio/src/slic3r/GUI/DeviceManager.cpp:1799` — `command_start_calibration(...)` bitfield
- `temp/references/BambuStudio/src/slic3r/GUI/DeviceCore/DevPrintOptions.cpp` — all `print_option` + `xcam_control_set` publishers
- `temp/references/BambuStudio/bbl/i18n/uk/BambuStudio_uk.po` — Ukrainian translations (matched by msgid)

## 3. Out of scope (this iteration)

- Printer Parts editing — read-only on every model, mirrors BS (PrinterPartsDialog calls `EnableEditing(false)` almost universally).
- In-UI audit-log viewer. Audit table is written but not surfaced.
- Calibration step-by-step wizard (BS has separate `CalibrationWizard*` pages for individual cali types). We only ship the all-in-one starter.
- Detailed AI sensitivity-tuning UX (level dropdowns ship, but no per-stage previews).

## 4. Architecture overview

```
Printer ─ MQTT push ─> bambu_mqtt parser ─> PrinterState (new fields)
                                                  |
                                                  v
                                          WebSocket update
                                                  |
                                                  v
Browser ⋯ kebab menu → PrinterSettingsModal (tabs) ←─ GET /printers/{id}/settings
                              |
                              v
                              POST {action,…} ── bambu_mqtt publisher ── MQTT → printer
                                                                                |
                                                                                v
                                                              printer_setting_audit insert (m061)
```

- **State source of truth:** the printer. We mirror MQTT push into `PrinterState`, never persist a "desired state."
- **3-second hold-timer** on every flag (backend + frontend) keeps optimistic toggles smooth.
- **`supports.*` map** computed from printer model + firmware version → drives UI row visibility (hidden when `false`).
- **Audit row** on every applied POST (new table `printer_setting_audit`, migration m061). Mirrors the AMS Settings pattern.
- **Calibration gate:** `printer.state ∈ {IDLE, FINISH, FAILED}` — otherwise 409.

## 5. Actions and MQTT payloads (from BS)

### 5.1 Calibration

One publisher; BS branches on firmware version:

```python
start_calibration(*, bed_leveling, vibration, motor_noise, flow_cali,
                  nozzle_cali=False, heatbed_cali=False, clumppos_cali=False)
```

- **X1 with rv1126 < 00.00.15.79** (legacy):
  ```json
  {"print": {"command": "gcode_file",
             "param": "/usr/etc/print/auto_cali_for_user.gcode",
             "sequence_id": "…"}}
  ```
- **Everyone else:**
  ```json
  {"print": {"command": "calibration",
             "sequence_id": "…",
             "option": <bitfield>}}
  ```
  Bits: 0=flow_cali, 1=bed_leveling, 2=vibration, 3=motor_noise, 4=nozzle_cali, 5=heatbed_cali, 6=clumppos_cali.

Stop: gcode `M999` via existing `send_gcode` (TBD-confirm during implementation — see Open Questions §11.1).

### 5.2 Print Options (printer-level toggles)

All via `print.command = "print_option"`:

| Field | Type | Method |
|---|---|---|
| `auto_recovery` | bool | `print_option_auto_recovery` |
| `sound_enable` | bool | `print_option_sound` |
| `filament_tangle_detect` | bool | `print_option_filament_tangle` |
| `nozzle_blob_detect` | bool | `print_option_nozzle_blob` |
| `xcam__save_remote_print_file_to_storage` | int | `print_option_save_remote_to_storage` |
| `air_purification` | int (0/1/2) | `print_option_purify_air` |
| `xcam_door_open_check` | int (0/1/2) | `print_option_open_door` |
| `build_plate_marker_detect` | bool | `print_option_plate_type` |
| `plate_align_check` | bool | `print_option_plate_align` |

### 5.3 XCam AI detections

One generic publisher via `xcam.command = "xcam_control_set"`:

```python
xcam_control_set(module_name: str, enabled: bool, sensitivity: str | None = None)
```

Payload shape:
```json
{"xcam": {"command": "xcam_control_set",
          "sequence_id": "…",
          "module_name": <name>,
          "control": <enabled>,
          "enable": <enabled>,
          "print_halt": true,
          "halt_print_sensitivity": <sensitivity if given>}}
```

`module_name` ∈ `{first_layer_inspector, spaghetti_detector, purgechutepileup_detector, nozzleclumping_detector, airprinting_detector, fod_check, displacement_detection}`.
`sensitivity` ∈ `{"low", "medium", "high"} | None`.

### 5.4 Camera snapshot

```json
{"camera": {"command": "ipcam_cap_pic_set",
            "sequence_id": "…",
            "control": "enable" | "disable"}}
```

### 5.5 Set nozzle (Parts)

Deferred to phase-2 (Parts is read-only this iteration). Publisher stub returns 409 if called. Payload research kept in Open Questions §11.2.

## 6. State reads (push parsing)

Extend `_handle_print_data` to surface new fields on `PrinterState`:

```python
state.print_options = {
    "auto_recovery":          bool | None,   # direct echo from print.auto_recovery
    "sound_enable":           bool | None,
    "filament_tangle_detect": bool | None,
    "nozzle_blob_detect":     bool | None,
    "save_remote_to_storage": int  | None,
    "air_purification":       int  | None,   # 0/1/2
    "open_door_check":        int  | None,   # 0/1/2
    "plate_type_detect":      bool | None,
    "plate_align_check":      bool | None,
    "snapshot_enabled":       bool | None,
    # AI detectors: read from existing xcam.cfg bit-parser
    "ai_monitoring":          {"enabled": bool|None, "sensitivity": str|None},
    "spaghetti":              {"enabled": bool|None, "sensitivity": str|None},
    "pileup":                 {"enabled": bool|None, "sensitivity": str|None},
    "clumping":               {"enabled": bool|None, "sensitivity": str|None},
    "airprint":               {"enabled": bool|None, "sensitivity": str|None},
    "first_layer":            {"enabled": bool|None, "sensitivity": str|None},
    "fod_check":              bool | None,
    "displacement":           bool | None,
}
state.calibration_active: bool   # derived: stg_cur >= 0
state.calibration_stage_current: int   # stg_cur
state.calibration_stages_planned: list[int]   # stg

state.printer_settings_hold: dict   # flag_name -> epoch_seconds, 3 s TTL
```

`state.stg` / `state.stg_cur` and `state.nozzles[]` are already parsed in the existing code — we surface them in the API response, no new parsing required.

XCam AI detectors already partially parsed in `_handle_xcam_data` (bits 5-16). Extend to expose:
- bit 17 → ? (TBD: research during implementation — sensitivity / halt-print)
- Direct fields from `print.print_option`: `auto_recovery`, `sound_enable`, `filament_tangle_detect`, `nozzle_blob_detect`, `air_purification`, `plate_align_check` (printer echoes these in push after a `print_option` command).

**Hold-timer** (same pattern as AMS Settings): publisher stamps `printer_settings_hold[flag] = now`; parser skips updating any flag where `now - hold < 3s`.

## 7. Backend layer

### 7.1 `bambu_mqtt.py` — new publishers

| Method | Notes |
|---|---|
| `start_calibration(...)` | Branches on `is_support_command_calibration()` per BS. |
| `stop_calibration()` | `send_gcode("M999\n")` (TBD-confirm). |
| `print_option_auto_recovery(bool)` | |
| `print_option_sound(bool)` | |
| `print_option_filament_tangle(bool)` | |
| `print_option_nozzle_blob(bool)` | |
| `print_option_save_remote_to_storage(int)` | |
| `print_option_purify_air(int)` | 0=Off, 1=Inside, 2=Outside. |
| `print_option_open_door(int)` | 0=Off, 1=Pause, 2=Halt. |
| `print_option_plate_type(bool)` | |
| `print_option_plate_align(bool)` | |
| `xcam_control_set(module, enabled, sensitivity=None)` | Generic AI/sensor toggle. |
| `camera_snapshot_enable(bool)` | |

All sync `def`, return `tuple[bool, str | None]` (success, sequence_id) — matches the shape we used in `ams_user_setting`. Each stamps `state.printer_settings_hold[flag_name] = time.time()`.

### 7.2 `services/printer_capabilities.py`

```python
def compute_printer_supports(state: PrinterState, printer_model: str | None,
                             module_vers: dict) -> PrinterSupports:
    ...
```

Returns flat dict with ~25 boolean keys. Sources:
- Hard-coded family→capability table (e.g. AI monitoring only X1+H2D family).
- Firmware version checks for calibration paths (X1 rv1126 ≥ 00.00.15.79 → modern bitfield).
- `printer_models.has_door_sensor(model)` reuse for `open_door_check` support.

Unit-testable in isolation against fixture state objects.

### 7.3 API router `routes/printer_settings.py`

```
GET  /printers/{printer_id}/settings
     Permission: PRINTERS_READ
     200: { tabs: { calibration, print_options, parts } }
     404: printer_not_found | printer_not_online

POST /printers/{printer_id}/settings
     Permission: PRINTERS_UPDATE
     Body: discriminated union by `action` (7 variants in §7.4)
     200: { ok: true, sequence_id: str | null }
     400: invalid_payload (Pydantic)
     404: printer_not_found | printer_not_online
     409: unsupported | printer_busy | calibration_already_running | no_active_calibration | parts_not_editable
     504: mqtt_publish_failed
```

Register in `main.py` next to `ams_settings_routes`.

### 7.4 Pydantic schemas `schemas/printer_settings.py`

```python
class CalibrationStartAction(BaseModel):
    action: Literal["calibration_start"]
    stages: list[Literal["bed_leveling", "vibration", "motor_noise",
                         "flow_cali", "nozzle_cali", "heatbed_cali",
                         "clumppos_cali"]]

class CalibrationStopAction(BaseModel):
    action: Literal["calibration_stop"]

class PrintOptionBoolAction(BaseModel):
    action: Literal["print_option_bool"]
    key: Literal["auto_recovery", "sound", "filament_tangle", "nozzle_blob",
                 "plate_type", "plate_align"]
    enabled: bool

class PrintOptionIntAction(BaseModel):
    action: Literal["print_option_int"]
    key: Literal["save_remote_to_storage", "purify_air", "open_door"]
    value: int = Field(ge=0, le=10)

class XCamControlAction(BaseModel):
    action: Literal["xcam_control"]
    module: Literal["first_layer_inspector", "spaghetti_detector",
                    "purgechutepileup_detector", "nozzleclumping_detector",
                    "airprinting_detector", "fod_check", "displacement_detection"]
    enabled: bool
    sensitivity: Literal["low", "medium", "high"] | None = None

class CameraSnapshotAction(BaseModel):
    action: Literal["camera_snapshot"]
    enabled: bool

class SetNozzleAction(BaseModel):
    action: Literal["set_nozzle"]
    nozzle_id: int = Field(ge=0, le=1)
    type: str
    diameter: float
    flow_type: str

PrinterSettingsPostBody = Annotated[
    CalibrationStartAction | CalibrationStopAction | PrintOptionBoolAction
    | PrintOptionIntAction | XCamControlAction | CameraSnapshotAction
    | SetNozzleAction,
    Field(discriminator="action"),
]
```

### 7.5 Migration `m061_printer_setting_audit.py`

```sql
CREATE TABLE printer_setting_audit (
    id INTEGER PRIMARY KEY,
    printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    tab TEXT NOT NULL,            -- 'calibration' | 'print_options' | 'parts'
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    sequence_id TEXT,
    result TEXT NOT NULL,         -- 'sent' | 'error'
    error_message TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX ix_printer_setting_audit_printer
    ON printer_setting_audit(printer_id, created_at DESC);
```

Postgres variant uses `SERIAL` + `now()`, same as `ams_setting_audit` (m060).

### 7.6 Model `models/printer_setting_audit.py`

SQLAlchemy 2.0 async model mirroring the table; registered in `models/__init__.py` so `Base.metadata.create_all()` picks it up on fresh installs.

## 8. Frontend

### 8.1 Entry point

Three-dots context menu (`MoreVertical`) on each printer card. Add menu item **"Printer Settings"** between **Edit Printer** and **Remove Printer**. Visible iff:
- `printer.connected === true`
- user has `printers:update`

Disabled with tooltip when offline.

### 8.2 `<PrinterSettingsModal>` component

Modal with three tabs:
- `<CalibrationTab>` — stage selection + Start/Stop, live progress via `state.stg`/`stg_cur`.
- `<PrintOptionsTab>` — grouped rows (AI Monitoring, Sensors, Door & Air, Behaviour, Build Plate). Each row hidden if `supports[key] === false`.
- `<PrinterPartsTab>` — read-only nozzle info; single column for most printers, two columns for H2D family.

Tab nav uses theme-aware classes (`bg-bambu-dark-secondary` for active, `text-bambu-gray` for inactive — same pattern as recent AMSSettingsModal fix).

Theme: every surface uses CSS variables (`bg-bambu-dark*`, `text-white`/`text-bambu-gray`, `accent-bambu-green` checkboxes, `border-bambu-dark-tertiary`) — full theme variant inheritance.

### 8.3 Hook `hooks/usePrinterSettings.ts`

TanStack Query against `GET /printers/{id}/settings` + a client-side per-flag hold-timer Map mirroring the backend 3 s window. Mutation function publishes the discriminated action.

### 8.4 Confirm dialogs (destructive)

- **Start calibration** — informative confirm: "Triggers physical printer calibration. Takes up to 20 min and blocks new prints during the run."
- **Stop calibration** — confirm: "Stop running calibration?"

Other toggles fire without confirm (mirrors BS).

### 8.5 Loading / null states

- First fetch: skeleton (animated `bg-bambu-dark`).
- Field `null` in state: row stays visible but checkbox `disabled` with tooltip "Waiting for printer status".
- `supports[key] === false`: row hidden entirely.
- User without `printers:update` reaching modal via deep-link: all toggles disabled, read-only banner.

## 9. i18n (en + uk)

Strings sourced from BS `_L(...)` calls (en) and `BambuStudio_uk.po` `msgstr` (uk). Empty `msgstr` → BamDude-original translation (no `// TBD-uk` markers, per the AMS Settings final state).

New `printerSettings` block in `frontend/src/i18n/locales/{en,uk}.ts` — full key list in §5 of the design discussion above.

## 10. Errors and edge cases

| Scenario | Behaviour |
|---|---|
| Printer offline | API 404; UI menu item disabled with tooltip "Printer offline" |
| Calibration on busy printer | API 409 `printer_busy`; UI shows BS-cited error copy |
| Unsupported toggle | API 409 `unsupported`; UI hides row anyway, double-gated |
| MQTT publish timeout | API 504; audit `result='error'`; UI: revert + toast |
| Push during 3 s optimistic hold | Backend and frontend both ignore the echo |
| Multi-tab open | WS invalidation syncs both tabs |
| Calibration interrupted by power loss | After reconnect, `stg_cur=-1` → UI unblocks automatically |
| User w/o `printers:update` via URL | GET 200, all disabled, banner |

## 11. Open questions / research deferred to implementation

1. **Stop-calibration MQTT shape.** BS doesn't seem to expose a dedicated stop — it relies on the generic print-stop. We default to `M999` via `send_gcode`. If stop doesn't reliably interrupt mid-calibration on real hardware, drop the Stop button (close-modal becomes the only "cancel" — calibration finishes on its own).
2. **Set-nozzle MQTT payload.** Parts is read-only this iteration; precise payload (likely under `system.command = ?`) deferred until edit support is requested.
3. **`xcam.cfg` bits 17+** — research during implementation: which bits map to `nozzle_blob`, `filament_tangle`, `fod_check`, `displacement`. Primary source planned is `print.print_option` direct echoes (mirrors how we surfaced `auto_switch_filament` in the AMS Settings parser); bit-parsing is a fallback if echo is too sparse.
4. **`open_door_check` SwitchBoard semantics.** BS uses a 3-state widget. We render it as Off / Pause / Halt; precise int mapping (0/1/2 vs 0/1/3) to confirm.

## 12. Tests

- `tests/unit/services/test_bambu_mqtt_printer_settings.py` — payload shape per publisher; hold-timer set; disconnect→False.
- `tests/unit/services/test_printer_capabilities.py` — model→supports map (X1, P1S, A1, A1 Mini, H2D, unknown).
- `tests/integration/test_m061_printer_setting_audit_migration.py` — table+index, idempotency.
- `tests/integration/test_printer_settings_routes.py` — auth gates, calibration_busy 409, audit row on success+error.
- `frontend/src/__tests__/components/PrinterSettingsModal.test.tsx` — vitest: skeleton → tabs render, hidden unsupported rows, calibration confirm gate, revert on error.

## 13. Rollout

- Single release, no feature flag — per-model `supports.*` keeps older / non-capable printers safe.
- CHANGELOG entry under `[Unreleased]` — concise, one-line summary form per the project's CHANGELOG rule.
- Docs: extend `docs.bamdude.top/docs/features/printers.md` (or sibling) with a short "Printer Settings dialog" section + uk variant.
- Landing: extend the existing "Printer management" or "AMS management" feature bullet to mention the new dialog. en + uk.

## 14. References to existing code

- `backend/app/services/bambu_mqtt.py:4787` — `ams_user_setting` (publisher style template — sync `def`, hold-timer stamp, `(bool, str|None)` return).
- `backend/app/api/routes/ams_settings.py` — router style (audit row pattern + discriminated union).
- `backend/app/services/ams_capabilities.py` — capability helper style (per-model frozen sets).
- `frontend/src/components/AMSSettingsModal.tsx` — modal style (theme classes, hold-timer hook integration).
- `frontend/src/hooks/useAmsSettings.ts` — hook style (TanStack Query + per-flag hold map).
