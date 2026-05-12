# AMS Settings Dialog — Design

**Date:** 2026-05-12
**Status:** Draft, awaiting user review
**Scope:** Port Bambu Studio `AMSSetting` dialog (everything in `BambuStudio/src/slic3r/GUI/AMSSetting.cpp`) into BamDude as a per-printer modal.

---

## 1. Goal

Surface, in BamDude's printer page, the same AMS-level toggles Bambu Studio exposes via *AMS Settings* — so farm operators can disable RFID-on-insert, RFID-on-startup, remaining-capacity estimation, filament auto-backup, air-print detection, and (where supported) switch AMS firmware mode (A1 LITE ↔ FULL) and re-arrange AMS unit IDs. The dialog is opened from a gear icon on the AMS panel of the printer page.

## 2. Authoritative source

All toggle semantics, MQTT payloads, gating logic, dialog copy, and confirm-dialog wording are taken **verbatim from Bambu Studio**:

- `temp/references/BambuStudio/src/slic3r/GUI/AMSSetting.cpp` — dialog source
- `temp/references/BambuStudio/src/slic3r/GUI/AMSSetting.hpp` — widget list
- `temp/references/BambuStudio/src/slic3r/GUI/DeviceManager.cpp:1575` — `command_ams_user_settings` (canonical payload)
- `temp/references/BambuStudio/src/slic3r/GUI/DeviceManager.cpp:1751` — `command_ams_switch_filament`
- `temp/references/BambuStudio/src/slic3r/GUI/DeviceManager.cpp:1765` — `command_ams_air_print_detect`
- `temp/references/BambuStudio/src/slic3r/GUI/DeviceManager.cpp:1595` — `command_ams_calibrate` (gcode `M620 C<ams_id>`)
- `temp/references/BambuStudio/src/slic3r/GUI/DeviceCore/DevFilaSystem.cpp:520-532` — push-message parsing of `ams.insert_flag` / `ams.power_on_flag` / `ams.calibrate_remain_flag`
- `temp/references/BambuStudio/src/slic3r/GUI/DeviceManager.cpp:1023,1032,1051` — `cfg` bitfield parsing for backup-enable (bit 10 X1 / bit 18 P1/A1) and air-print support (bit 29)
- `temp/references/BambuStudio/bbl/i18n/uk/BambuStudio_uk.po` — Ukrainian translations source (extract by `msgid`)

## 3. Out of scope (this iteration)

- An in-UI audit-log viewer. The audit table is created and written to but not surfaced.
- Backwards-port for printers that never report cfg-bits / capability flags (i.e. very old firmware): we degrade gracefully (`supports.*=false`) and hide the rows.

## 4. Architecture overview

```
Bambu printer ── MQTT push ──> bambu_mqtt.py parser ──> printer_state[id]["ams_system_setting"]
                                                                |
                                                                v
                                                       WebSocket update
                                                                |
                            Frontend AMS panel ⚙️ -> AMSSettingsModal <─ GET /printers/{id}/ams/settings
                                          \
                                           POST /printers/{id}/ams/settings {action, ...}
                                                                |
                                                                v
                                                      bambu_mqtt.py publish + audit insert
                                                                |
                                                                v
                                                      MQTT publish ── command ──> Bambu printer
```

- **State source of truth:** the printer. We only mirror.
- **No DB-persisted "desired state."** We don't reconcile: if the printer drops a setting we don't re-apply it. The audit table (§7.5) records *what was sent* for forensic trace, not a desired state to reconcile against.
- **Audit row** on every `POST` for forensic trace (since BS has no RBAC and we expose this under `PRINTERS_UPDATE`).

## 5. Actions and MQTT payloads (verbatim from BS)

| Action | MQTT command | Payload |
|---|---|---|
| `user_setting` | `print.command = "ams_user_setting"` | `ams_id: -1`, `startup_read_option: bool`, `tray_read_option: bool`, `calibrate_remain_flag: bool`, `sequence_id` |
| `auto_switch_filament` | `print.command = "print_option"` | `auto_switch_filament: bool`, `sequence_id` |
| `air_print_detect` | `print.command = "print_option"` | `air_print_detect: bool`, `sequence_id` |
| `calibrate` | gcode line | `M620 C<ams_id>` wrapped via existing `gcode_claim_action` |
| `firmware_switch` | TBD — research `DevFilaAmsSettingCtrl::CrtlSwitchFirmware` body before implementing | uses `firmware_idx` |
| `reorder` | TBD — research `AMSSettingArrangeAMSOrder::OnBtnRearrangeClicked` body before implementing | resets connected-AMS ID sequence |

The first three are issued from a single dialog. `user_setting` always carries all three flags together (the BS handler reads current widget state for all three on every change). On our side: the POST body for `user_setting` likewise carries all three; the frontend reads current local state for the other two when one is toggled.

## 6. State reads (push parsing)

Extend `bambu_mqtt.py` parser to populate `printer_state[id]["ams_system_setting"]`:

```python
{
  "insertion_update":      bool | None,   # print.ams.insert_flag
  "power_on_update":       bool | None,   # print.ams.power_on_flag
  "remain_capacity":       bool | None,   # print.ams.calibrate_remain_flag
  "auto_switch_filament":  bool | None,   # cfg bit 10 (X1) / bit 18 (P1, A1)
  "air_print_detect":      bool | None,   # print.air_print_detect (or print.option_*)
  "firmware_idx_run":      int  | None,   # current running AMS firmware index
  "firmware_idx_sel":      int  | None,   # selected index (may differ during switch)
  "supports": {
    "insertion_update":      bool,
    "power_on_update":       bool,
    "remain_capacity":       bool,
    "auto_switch_filament":  bool,
    "air_print_detect":      bool,
    "firmware_switch":       bool,
    "reorder":               bool,
  }
}
```

**Hold-timer (BS pattern):** when the backend publishes any of the above commands, record `printer_state[id]["_ams_settings_hold"][flag] = now`. Parser skips updating a flag from push when `now - hold < 3s`. This avoids the toggle flipping back during the half-second between command send and printer-confirmed echo.

**`supports` derivation:**

- `insertion_update`: True iff AMS has RFID (i.e. not A1 LITE firmware, not AMS HT/`f1`). For AMS F1 with sw `>= 00.00.07.89`, BS auto-enables and hides the toggle — we replicate.
- `power_on_update`: same gate as `insertion_update`.
- `remain_capacity`: True iff printer reports `support_update_remain == true` AND `support_update_remain_hide_display != true`.
- `auto_switch_filament`: True iff printer reports `is_support_filament_backup` (printer-config flag + `cfg` bit semantics).
- `air_print_detect`: True iff printer reports `is_support_air_print_detection` AND `air_print_detection_position == "ams_setting"` (A1/A1 mini only — other models show this in Print Options elsewhere).
- `firmware_switch`: True iff `DevAmsSystemFirmwareSwitch::SupportSwitchFirmware()` is true for this printer (A1 series with multi-firmware build).
- `reorder`: True iff printer-config `support_ams_settings_reorder == true` (typically H2D + multi-AMS setups).

Where BS reads a printer-config JSON (`support_*`) keyed by `printer_type`, we either port that lookup table from `BambuStudio/resources/printers/*.json` (preferred — single source of truth, machine-readable) or hardcode a small model→capabilities map in `bambu_mqtt.py`. Decision deferred to the implementation plan, with porting as default.

## 7. Backend layer

### 7.1 `bambu_mqtt.py` (extend)

Add these methods, mirroring existing `ams_*` shape:

```python
async def ams_user_setting(self, startup_read: bool, tray_read: bool, calibrate_remain: bool) -> str | None
async def print_option_auto_switch_filament(self, enabled: bool) -> str | None
async def print_option_air_print_detect(self, enabled: bool) -> str | None
async def ams_calibrate(self, ams_id: int) -> None
async def ams_firmware_switch(self, firmware_idx: int) -> str | None    # TBD payload
async def ams_reorder(self, order: list[int]) -> str | None              # TBD payload
```

Each returns the `sequence_id` (when applicable) so callers can correlate with audit / future receipt tracking.

### 7.2 Parser extension

In `_handle_print_data` (or whatever helper consumes `print.*`):

- Read `ams.insert_flag` / `ams.power_on_flag` / `ams.calibrate_remain_flag` if present (only when no active hold for that flag).
- Read `cfg` int → derive bits 10 (X1 backup), 18 (P1/A1 backup), 29 (air-print support). Model selects which bit to consult — table from BS.
- Read `print.air_print_detect` if present. Printers echo this field back in their state pushes after a `print_option` command lands; we treat the most recent echo as authoritative (subject to the hold-timer below).
- Compute and store `supports.*` once we have printer model + `cfg` + firmware versions.

WebSocket layer must broadcast `ams_system_setting` updates as part of the existing printer-state diff (no new channel needed — just include the new dict).

### 7.3 API router `backend/app/api/routes/ams_settings.py`

```
GET  /printers/{printer_id}/ams/settings
     RequirePermission(Permission.PRINTERS_VIEW)
     200 { state, supports, firmware_options, ams_units }
     404 if printer not found / not online
```

```
POST /printers/{printer_id}/ams/settings
     RequirePermission(Permission.PRINTERS_UPDATE)
     Body (discriminated by `action`):
       { "action": "user_setting",
         "startup_read_option": bool, "tray_read_option": bool, "calibrate_remain_flag": bool }
       { "action": "auto_switch_filament", "enabled": bool }
       { "action": "air_print_detect", "enabled": bool }
       { "action": "calibrate", "ams_id": int }
       { "action": "firmware_switch", "firmware_idx": int }
       { "action": "reorder", "order": [int] }
     200 { ok: true, sequence_id: str | null }
     404 printer_not_found / printer_not_online
     409 unsupported (supports[action]=false)
     412 precondition_failed (printing, unload required)
     423 printer_busy (e.g. mid-print firmware_switch)
     400 invalid_payload
```

Register in `main.py` next to other AMS-touching routers.

### 7.4 Pydantic schemas

`backend/app/schemas/ams_settings.py`:

- `AmsSystemSettingState` — the `state` dict.
- `AmsSystemSettingSupports` — the `supports` dict.
- `AmsSettingsGetResponse` — wraps both plus `firmware_options`, `ams_units`.
- Six action body models + a `Union` discriminator on `action`.

### 7.5 Migration `mNNN_add_ams_setting_audit.py`

NNN is the next free version (058+, exact number set during implementation).

```sql
CREATE TABLE IF NOT EXISTS ams_setting_audit (
  id INTEGER PRIMARY KEY,
  printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
  user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  action TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  sequence_id TEXT,
  result TEXT NOT NULL,          -- 'sent' | 'error'
  error_message TEXT,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_ams_setting_audit_printer
  ON ams_setting_audit(printer_id, created_at DESC);
```

Migration docstring describes the *why* (RBAC trace of AMS settings changes — BS has no audit; we add one because PRINTERS_UPDATE-level ops can affect every operator's workflow on the farm).

Model file `backend/app/models/ams_setting_audit.py` with the same columns plus relationships back to `Printer` and `User` (lazy='raise'). Imported in `database.py`.

### 7.6 Audit recording

In the POST handler: after the MQTT publish call returns (success or raise), insert one row. `payload_json` is the raw request body (sanitized via `model_dump`). `sequence_id` from MQTT layer when present.

## 8. Frontend

### 8.1 Entry point

Gear icon (`Cog6Tooth` Heroicon) in the AMS-panel header on the printer page. Visible iff:
- printer is `online`
- `printer_state.ams.ams_exist_bits != 0`
- user has `printers:update`

Tooltip "AMS Settings" (i18n: `amsSettings.title`).

### 8.2 `AMSSettingsModal.tsx`

Layout (BS-style, top to bottom, separators between blocks):

1. **Insertion update** (checkbox + 1-3 tip lines that swap on toggle).
2. **Power on update** (checkbox + 1-2 tip lines).
3. **Update remaining capacity** (checkbox + 1 tip).
4. **AMS filament backup** (checkbox + 1 tip).
5. **Air Printing Detection** (checkbox + 1 tip) — only if `supports.air_print_detect`.
6. **AMS Type** combo + Switch button — only if `supports.firmware_switch`.
7. **Arrange AMS Order** Reset button — only if `supports.reorder`.
8. **Calibrate AMS** unit-dropdown + button (always shown when ≥1 AMS).
9. **AMS hero illustration** (reuse existing `ams_icon` asset if present).

### 8.3 Data flow

- TanStack Query `useAmsSettings(printerId)` → `GET /printers/{id}/ams/settings`.
- WebSocket existing printer-state hook → when `ams_system_setting` field changes, `queryClient.invalidate(['ams-settings', id])`.
- Toggle handler:
  ```ts
  const onToggle = async (flag, next) => {
    setLocalOverride(flag, next);                 // optimistic
    try {
      await api.postAmsSettings(id, payload);
      startClientHold(flag, 3000);                // ignore WS echoes for 3s
    } catch (e) {
      setLocalOverride(flag, !next);              // revert
      toast.error(t(`ams.errors.${e.code}`));
    }
  };
  ```
- For confirm-gated destructive actions (`firmware_switch`, `reorder`): show `ConfirmDialog` with BS copy before issuing POST.

### 8.4 Loading and empty states

- First fetch: skeleton checkboxes (greyed pulse).
- Field is `null` in `state` (no push received yet): row shows `—` and toggle is `disabled` with tooltip "Waiting for printer status".
- `supports.*=false`: row hidden entirely (BS behavior).
- User has only `printers:view`: dialog still opens via gear (we keep gate strict — gear only shown for `manage`). If the user navigated via API directly, the GET returns 200 and frontend shows everything `disabled` with a "read-only" banner.

### 8.5 Hold-timer (client)

Per-flag `Map<flag, deadline>`. WS-derived state for a flag is ignored while `deadline > now`. Mirrors the server-side hold so visual stays stable.

## 9. i18n (en + uk strictly from BS)

All visible text comes from BS `_L(...)` strings (see source-cites in §2). Ukrainian translations are extracted from `BambuStudio/bbl/i18n/uk/BambuStudio_uk.po` by matching `msgid`. If a `msgid` has empty `msgstr` (untranslated), the corresponding key is added to a `TBD list` in the implementation plan and resolved with the user (no fabricated translations).

New i18n block in `frontend/src/i18n/locales/{en,uk}.ts`:

```ts
amsSettings: {
  title:                  "AMS Settings",
  insertionUpdate:        "Insertion update",
  insertionUpdateTipOn:   "The AMS will automatically read the filament information when inserting a new Bambu Lab filament. This takes about 20 seconds.",
  insertionUpdateTipNote: "Note: if a new filament is inserted during printing, the AMS will not automatically read any information until printing is completed.",
  insertionUpdateTipOff:  "When inserting a new filament, the AMS will not automatically read its information, leaving it blank for you to enter manually.",
  powerOnUpdate:          "Power on update",
  powerOnTipOn:           "The AMS will automatically read the information of inserted filament on start-up. It will take about 1 minute. The reading process will roll filament spools.",
  powerOnTipOff:          "The AMS will not automatically read information from inserted filament during startup and will continue to use the information recorded before the last shutdown.",
  updateRemain:           "Update remaining capacity",
  updateRemainTip:        "AMS will attempt to estimate the remaining capacity of the Bambu Lab filaments.",
  filamentBackup:         "AMS filament backup",
  filamentBackupTip:      "AMS will continue to another spool with the same properties of filament automatically when current filament runs out",
  airPrintDetection:      "Air Printing Detection",
  airPrintTip:            "Detects clogging and filament grinding, halting printing immediately to conserve time and filament.",
  amsType:                "AMS Type",
  arrangeOrder:           "Arrange AMS Order",
  arrangeNote:            "If you want a specific AMS ID sequence, please disconnect all AMS after clicking 'Reset', and then reconnect them in the desired order.",
  reset:                  "Reset",
  confirmReorder:         "Are you sure to reset the ID sequence of the connected AMS?",
  reorderTitle:           "Reset AMS Sequence",
  busyCantSwitch:         "The printer is busy and cannot switch AMS type.",
  unloadBeforeSwitch:     "Please unload all filament before switching.",
  switchFirmwareConfirm:  "AMS type switching needs firmware update, taking about 30s. Switch now?",
  switchProgress:         "Switching {percent}%",
  confirm:                "Confirm",
  calibrate:              "Calibrate AMS",
  selectAmsForCalibrate:  "AMS unit",
}
```

Uk values pulled from `BambuStudio_uk.po`. Empty `msgstr` → flagged TBD.

## 10. Errors and edge cases

| Scenario | Behavior |
|---|---|
| Printer offline | API 404 `printer_not_online`; UI: gear disabled with tooltip "Printer offline" |
| MQTT publish timeout | API 504; audit row `result='error'`; UI: toast + revert |
| Printer busy for `firmware_switch` / `reorder` | API 423; UI confirm-modal with `busyCantSwitch` copy |
| Filament loaded when firmware_switch requested | API 412 `unload_required`; UI shows `unloadBeforeSwitch` |
| Printer doesn't report `cfg` (old firmware) | `supports.*=false`; rows hidden; modal shows "Update printer firmware to manage these" |
| Push arrives before POST returns (race) | 3s hold-timer on backend AND client suppresses the echo on the specific flag |
| User with `printers:view` only opens dialog (URL-direct) | GET 200, all toggles disabled, read-only banner |
| Multi-AMS H2D | `user_setting` still uses `ams_id=-1`; auto-demarcate uses per-AMS `ams_id` from dropdown |

## 11. Open questions / research needed at implementation time

These do not block the design but the implementation plan must include a "research" task before the corresponding feature:

1. **Payload for `ams_firmware_switch`** — **resolved** from `DevFilaAmsSettingCtrl.cpp:7-28`:
   ```json
   {"upgrade": {"command": "mc_for_ams_firmware_upgrade", "sequence_id": "…", "src_id": 1, "id": <firmware_idx>}}
   ```
   Note: under `upgrade` key, NOT `print`. `src_id=1` identifies Bambu Studio (BamDude can reuse 1).
2. **Payload for `ams_reorder`** — **resolved** from `DevFilaSystemCtrl.cpp:11-17`:
   ```json
   {"print": {"command": "ams_reset", "sequence_id": "…"}}
   ```
   No `order` array — BS sends a bare reset; the dialog instructs the user to physically disconnect+reconnect AMS units in the desired order. Our action body therefore has no payload either.
3. **`air_print_detection_position` gating** — port the printer-config JSON lookup from `BambuStudio/resources/printers/*.json`, or hardcode the A1/A1-mini whitelist.
4. **Auto-switch-filament cfg-bit** — verify against a real printer log whether X1 uses bit 10 and P1/A1 use bit 18, and pick correct bit per `printer.model_code`.

## 12. Tests

- **`backend/tests/unit/services/test_bambu_mqtt_ams_settings.py`** — payload construction, hold-timer, cfg-bit parsing for X1/P1/A1.
- **`backend/tests/integration/api/test_ams_settings_routes.py`** — auth gates, GET/POST happy paths, 409/412/423 for unsupported/preconditions, audit row insertion.
- **`backend/tests/unit/migrations/test_mNNN_ams_setting_audit.py`** — migration smoke + idempotency.
- **`frontend/src/components/__tests__/AMSSettingsModal.test.tsx`** — skeleton, supports-driven hide, toggle → POST → optimistic, confirm-modal for destructive actions, error revert.

## 13. Rollout

- **Single release.** No feature flag — gate is the per-printer `supports.*`, which keeps the UI safe for older printers automatically.
- **CHANGELOG entry** under existing `0.4.4` section: "feat(ams): AMS settings dialog (RFID/backup/calibrate/firmware/reorder) — parity with Bambu Studio."
- **Docs:** add a short section to `docs/printers.md` (or equivalent) — only after merge.

## 14. References to existing code

- `backend/app/services/bambu_mqtt.py:4478` — existing `ams_load_filament` (style template).
- `backend/app/services/bambu_mqtt.py:4661` — existing `ams_set_filament_setting` (payload-construction style).
- `backend/app/api/routes/printers.py:2966` — existing AMS slot-configure route (auth / 404 pattern).
- `frontend/src/components/ConfigureAmsSlotModal.tsx` — modal style template.
- `frontend/src/utils/amsHelpers.ts` — `formatSlotLabel`, model-aware AMS labelling (reuse for the calibrate-AMS dropdown).
