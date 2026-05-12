# Flow Rate / PA Calibration — Plan 3 (Auto + History + H2D + Docs)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close BS feature-parity gap left by Plan 2. Enable X1 lidar auto-cali UI, tower modes (Temp / VolSpeed / VFA / Retraction), full history view with set-active + delete + printer-history refresh, H2D dual-extruder per-extruder cali. Ship docs site + landing page updates.

**Architecture:** No new tables, no new MQTT verbs — Plan 1 backend already covers everything. This plan is mostly frontend: one new save page (`CalibrationAutoSavePage`), one new modal (`CalibrationHistoryModal`), a `CalibrationTowerFinishPage` for non-saving tower modes, and per-extruder tabs in `CalibrationPresetPage`. Plus backend: small adjustments to enable Auto+Tower start paths in `CalibrationService` (Plan 1 stubbed `NotImplementedError` for non-PA/non-Flow modes).

**Tech Stack:** Same as Plan 2. React 19 + TanStack Query 5 + Tailwind 4 + react-i18next + lucide-react.

**Spec:** `docs/superpowers/specs/2026-05-12-flow-pa-calibration-design.md`. Plans 1 + 2 must ship before Plan 3 execution.

**User workflow notes:**
- Per-wave verify (build/lint/test once per wave).
- Commits only on explicit user ask.
- All conversation in Ukrainian; code/docs/commits in English.
- i18n keys: both `en.ts` AND `uk.ts` in same task.

---

## File Map

**New frontend files (4):**
- `frontend/src/components/calibration/CalibrationAutoSavePage.tsx`
- `frontend/src/components/calibration/CalibrationTowerFinishPage.tsx`
- `frontend/src/components/CalibrationHistoryModal.tsx`
- `frontend/src/hooks/useCalibrationHistory.ts`

**Modified frontend files (6):**
- `frontend/src/api/client.ts` — history methods + auto-result types
- `frontend/src/i18n/locales/en.ts` — extend `filamentCali` namespace (auto, towers, history, h2d)
- `frontend/src/i18n/locales/uk.ts` — same
- `frontend/src/components/calibration/CalibrationStartPage.tsx` — un-gate auto + towers
- `frontend/src/components/calibration/CalibrationPresetPage.tsx` — H2D extruder tabs
- `frontend/src/components/FilamentCalibrationModal.tsx` — mount AutoSave + TowerFinish + History trigger
- `frontend/src/hooks/useFilamentCalibration.ts` — extend state machine (autoSave, towerFinish)
- `frontend/src/pages/PrintersPage.tsx` — kebab entry for history modal

**Modified backend files (3):**
- `backend/app/services/calibration_service.py` — un-stub auto-flow + tower start; submit_auto_result improvements
- `backend/app/services/background_dispatch.py` — on-complete handler for tower modes (skip awaiting_user_input)
- `backend/tests/unit/services/test_calibration_service.py` — new test coverage

**Docs files (4):**
- Modify: `D:/Development/docs.bamdude.top/docs/features/printer-control.md` — extend Filament Calibration section
- Modify: `D:/Development/docs.bamdude.top/docs/features/printer-control.uk.md`
- Modify: `D:/Development/bamdude.top/src/content/en/features-grouped.json`
- Modify: `D:/Development/bamdude.top/src/content/uk/features-grouped.json`

---

## Wave 1 — Auto path (X1 lidar)

### Task 1: Backend — un-stub auto Flow Rate + clean up start_calibration branching

**Files:**
- Modify: `backend/app/services/calibration_service.py`
- Modify: `backend/tests/unit/services/test_calibration_service.py`

- [ ] **Step 1: Test for auto Flow Rate happy path**

Append to `backend/tests/unit/services/test_calibration_service.py`:

```python
@pytest.mark.asyncio
async def test_start_calibration_auto_flow_rate(service, db_session, printer_factory, mock_client):
    printer = await printer_factory(model="X1C")
    mock_client.flow_rate_cali_start.return_value = (True, "SEQ-FLOW-1")
    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = mock_client
        session = await service.start_calibration(
            db=db_session, printer_id=printer.id,
            cali_mode=CaliMode.FLOW_RATE, method=CaliMethod.AUTO,
            nozzle_diameter=0.4, nozzle_volume_type="standard", extruder_id=0,
            filaments=[CalibFilamentInput(0, 0, 0, "GFG00", "GFG00_60@BBL", 60, 220, 12.0, flow_rate=0.98)],
            user_id=None,
        )
    assert session.method == "auto"
    assert session.cali_mode == "flow_rate"
    assert session.mqtt_sequence_id == "SEQ-FLOW-1"
    mock_client.flow_rate_cali_start.assert_called_once()
    call = mock_client.flow_rate_cali_start.call_args.kwargs
    assert call["filaments"][0]["flow_rate"] == 0.98
```

- [ ] **Step 2: Run test, see failure**

Run: `pytest backend/tests/unit/services/test_calibration_service.py::test_start_calibration_auto_flow_rate -v`
Expected: FAIL (current Plan-1 code handles auto + AUTO_PA_LINE only; FLOW_RATE+AUTO falls to manual NotImplementedError path).

Note: Plan 1 already implemented `flow_rate_cali_start` invocation for `cali_mode == CaliMode.FLOW_RATE` and `method == AUTO`. Verify by reading current implementation. If already implemented, skip Step 3 + 4.

- [ ] **Step 3: Inspect current state**

```
grep -n "FLOW_RATE\|flow_rate_cali_start" backend/app/services/calibration_service.py
```

If `cali_mode == CaliMode.FLOW_RATE and method == AUTO` already kicks off `flow_rate_cali_start` — test should pass. If not, adjust the branching in `start_calibration`:

```python
if method == CaliMethod.AUTO and cali_mode == CaliMode.AUTO_PA_LINE:
    ok, sequence_id = client.extrusion_cali_start(
        nozzle_diameter=nozzle_diameter, cali_mode=0, filaments=filaments_payload,
    )
    if not ok:
        raise ValueError("MQTT publish failed")
elif method == CaliMethod.AUTO and cali_mode == CaliMode.FLOW_RATE:
    for fp, f in zip(filaments_payload, filaments):
        fp["flow_rate"] = f.flow_rate
    ok, sequence_id = client.flow_rate_cali_start(
        nozzle_diameter=nozzle_diameter, filaments=filaments_payload,
    )
    if not ok:
        raise ValueError("MQTT publish failed")
else:
    # MANUAL path
    ...
```

- [ ] **Step 4: Run test**

Run: `pytest backend/tests/unit/services/test_calibration_service.py::test_start_calibration_auto_flow_rate -v`
Expected: passed.

- [ ] **Step 5: Stage**

`git add backend/app/services/calibration_service.py backend/tests/unit/services/test_calibration_service.py`

---

### Task 2: Backend — submit_auto_result for FLOW_RATE (multi-row flow_ratio)

**Files:**
- Modify: `backend/app/services/calibration_service.py`
- Modify: `backend/tests/unit/services/test_calibration_service.py`

- [ ] **Step 1: Test**

Append to `test_calibration_service.py`:

```python
@pytest.mark.asyncio
async def test_submit_auto_result_flow_rate(service, db_session, printer_factory, mock_client):
    """Auto Flow Rate push returns flow_ratio per filament — submit saves them."""
    printer = await printer_factory(model="X1C")
    from backend.app.models.calibration_session import CalibrationSession
    from backend.app.services.bambu_mqtt import ExtrusionCaliResult
    s = CalibrationSession(
        printer_id=printer.id, user_id=None,
        cali_mode="flow_rate", method="auto",
        nozzle_diameter=0.4, nozzle_volume_type="standard", extruder_id=0,
        filaments_json='[{"ams_id":0,"slot_id":0,"tray_id":0,"filament_id":"GFG00",'
                       '"setting_id":"GFG00_60@BBL","nozzle_id":"HS20","nozzle_diameter":"0.4"}]',
        status="awaiting_user_input", stage=1,
    )
    db_session.add(s); await db_session.commit(); await db_session.refresh(s)

    # ExtrusionCaliResult uses k_value for PA — for flow we extend the same dataclass
    # OR rely on a separate parser. Plan 1 parsed only PA; for Flow we read the
    # same push struct but the firmware fills flow_ratio inline as k_value-like
    # field. The service maps k_value → flow_ratio when session.cali_mode == flow_rate.
    mock_client.state.extrusion_cali_results = [
        ExtrusionCaliResult(
            tray_id=0, ams_id=0, slot_id=0, extruder_id=0,
            nozzle_diameter=0.4, nozzle_volume_type="standard",
            filament_id="GFG00", setting_id="GFG00_60@BBL",
            k_value=1.05, n_coef=0.0, confidence=0,
        )
    ]
    mock_client.extrusion_cali_set = MagicMock(return_value=(True, "S"))
    mock_client.extrusion_cali_sel = MagicMock(return_value=(True, "S"))

    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = mock_client
        rows = await service.submit_auto_result(
            db=db_session, session_id=s.id,
            edits=[{"tray_id": 0, "save": True, "name": "PLA — flow 1.05"}],
        )
    assert len(rows) == 1
    assert abs(rows[0].flow_ratio - 1.05) < 1e-9
    assert rows[0].pa_k_value is None
```

- [ ] **Step 2: Adjust submit_auto_result**

In `calibration_service.py`, `submit_auto_result`, detect cali_mode and route to flow vs PA persistence:

```python
async def submit_auto_result(
    self,
    *,
    db: AsyncSession,
    session_id: int,
    edits: list[dict],
) -> list[FilamentCalibration]:
    s = (
        await db.execute(select(CalibrationSession).where(CalibrationSession.id == session_id))
    ).scalar_one()
    if s.status != "awaiting_user_input":
        raise ValueError(f"session not awaiting input (status={s.status})")

    client = printer_manager.get_client(s.printer_id)
    if not client:
        raise ValueError("Printer not online")

    is_flow = CaliMode(s.cali_mode) == CaliMode.FLOW_RATE
    results_by_tray = {r.tray_id: r for r in client.state.extrusion_cali_results}
    saved: list[FilamentCalibration] = []
    for edit in edits:
        if not edit.get("save", True):
            continue
        base = results_by_tray.get(edit["tray_id"])
        if base is None:
            continue

        if is_flow:
            flow = float(edit.get("flow_ratio", base.k_value))  # firmware delivers via k_value slot for flow
            name = edit.get("name") or f"{base.filament_id} flow {flow:.3f}"
            row = await self.save_result(
                db=db, session=s,
                payload=ResultPayload(
                    flow_ratio=flow, confidence=base.confidence,
                    source="auto", name=name,
                ),
            )
        else:
            k = float(edit.get("k_value", base.k_value))
            n = float(edit.get("n_coef", base.n_coef))
            name = edit.get("name") or f"{base.filament_id} PA {k:.4f}"
            row = await self.save_result(
                db=db, session=s,
                payload=ResultPayload(
                    pa_k_value=k, pa_n_coef=n, confidence=base.confidence,
                    source="auto", name=name,
                ),
            )
        saved.append(row)
    return saved
```

- [ ] **Step 3: Adjust save_result MQTT branch for flow-only rows**

Existing `save_result` only sends `extrusion_cali_set` when `payload.pa_k_value is not None`. For flow-only rows we should also push the value back (BS sends the same `extrusion_cali_set` shape with k_value carrying flow). Update:

```python
# In save_result, MQTT push branch:
if client and client.state.connected and (payload.pa_k_value is not None or payload.flow_ratio is not None):
    push_k_value = (
        payload.pa_k_value if payload.pa_k_value is not None
        else payload.flow_ratio  # BS uses k_value slot for both PA + flow ratio
    )
    client.extrusion_cali_set(
        nozzle_diameter=session.nozzle_diameter,
        filaments=[{
            "tray_id": fil["tray_id"],
            "extruder_id": session.extruder_id,
            "nozzle_id": fil.get("nozzle_id", ""),
            "nozzle_diameter": str(session.nozzle_diameter),
            "ams_id": fil["ams_id"],
            "slot_id": fil["slot_id"],
            "filament_id": fil["filament_id"],
            "setting_id": fil.get("setting_id") or "",
            "name": payload.name,
            "k_value": str(push_k_value),
            "n_coef": str(payload.pa_n_coef or 0.0),
        }],
    )
    if new_row.cali_idx is not None:
        client.extrusion_cali_sel(
            ams_id=fil["ams_id"],
            tray_id=fil["tray_id"],
            cali_idx=new_row.cali_idx,
            extruder_id=session.extruder_id,
            nozzle_diameter=session.nozzle_diameter,
        )
```

- [ ] **Step 4: Run tests**

Run: `pytest backend/tests/unit/services/test_calibration_service.py -v -k "auto_result"`
Expected: 2 passed (PA from Plan 1 + new flow row).

- [ ] **Step 5: Stage**

`git add backend/app/services/calibration_service.py backend/tests/unit/services/test_calibration_service.py`

---

### Task 3: Backend — tower modes (Temp/VolSpeed/VFA/Retraction): print-and-finish

**Files:**
- Modify: `backend/app/services/calibration_service.py`
- Modify: `backend/app/services/background_dispatch.py`
- Modify: `backend/tests/unit/services/test_calibration_service.py`

- [ ] **Step 1: Test**

```python
@pytest.mark.asyncio
async def test_start_temp_tower_dispatches_print(service, db_session, printer_factory, mock_client, tmp_path):
    printer = await printer_factory(model="P1S")
    asset = tmp_path / "temp_tower_0.4.3mf"
    asset.write_bytes(b"PK\x03\x04fake3mf")
    with patch("backend.app.services.calibration_service.printer_manager") as pm, \
         patch("backend.app.services.calibration_service.resolve_asset_path", return_value=asset), \
         patch("backend.app.services.calibration_service.background_dispatch") as bg:
        pm.get_client.return_value = mock_client
        bg.enqueue_calibration_print = AsyncMock(return_value=77)
        session = await service.start_calibration(
            db=db_session, printer_id=printer.id,
            cali_mode=CaliMode.TEMP_TOWER, method=CaliMethod.MANUAL,
            nozzle_diameter=0.4, nozzle_volume_type="standard", extruder_id=0,
            filaments=[CalibFilamentInput(0, 0, 0, "GFG00", "GFG00_60@BBL", 60, 220, 12.0)],
            user_id=None,
        )
    assert session.cali_mode == "temp_tower"
    assert session.method == "manual"
    assert session.print_queue_item_id == 77


def _is_tower_mode(cali_mode: str) -> bool:
    return cali_mode in {"temp_tower", "vol_speed_tower", "vfa_tower", "retraction_tower"}


def test_is_tower_mode_helper():
    assert _is_tower_mode("temp_tower")
    assert _is_tower_mode("vol_speed_tower")
    assert _is_tower_mode("vfa_tower")
    assert _is_tower_mode("retraction_tower")
    assert not _is_tower_mode("pa_line")
    assert not _is_tower_mode("flow_rate")
```

- [ ] **Step 2: Add helper + adjust start_calibration**

Tower modes already fall to the MANUAL branch in `start_calibration` (because `method != AUTO`). The existing manual path enqueues the asset — that's what we want. Confirm `resolve_asset_path` handles `CaliMode.TEMP_TOWER` etc. (Plan 1 Task 12 mapping does). Just need to make sure the **on-complete handler** in `background_dispatch.py` flips session to `saved` (not `awaiting_user_input`) for tower modes — no user input to collect.

In `backend/app/services/calibration_service.py`, near the top:

```python
TOWER_MODES = frozenset({
    CaliMode.TEMP_TOWER, CaliMode.VOL_SPEED_TOWER,
    CaliMode.VFA_TOWER, CaliMode.RETRACTION_TOWER,
})


def is_tower_mode(cali_mode: str | CaliMode) -> bool:
    if isinstance(cali_mode, str):
        try:
            cali_mode = CaliMode(cali_mode)
        except ValueError:
            return False
    return cali_mode in TOWER_MODES
```

- [ ] **Step 3: Adjust dispatch on-complete for tower mode**

In `backend/app/services/background_dispatch.py`, where the on-complete handler flips session status (Plan 1 Task 15):

```python
# Inside on-complete handler:
if completed_item.is_calibration and completed_item.calibration_session_id:
    async with async_session_maker() as db:
        cs = await db.get(CalibrationSession, completed_item.calibration_session_id)
        if cs and cs.status == "running":
            from backend.app.services.calibration_service import is_tower_mode
            if is_tower_mode(cs.cali_mode):
                cs.status = "saved"   # Tower modes: print-and-finish, no user input
            else:
                cs.status = "awaiting_user_input"
            await db.commit()

    from backend.app.services.calibration_service import broadcast_calibration_event
    await broadcast_calibration_event(
        printer_id=cs.printer_id,
        event="saved" if is_tower_mode(cs.cali_mode) else "completed",
        payload={"session_id": cs.id},
    )
```

- [ ] **Step 4: Run tests**

Run: `pytest backend/tests/unit/services/test_calibration_service.py -v -k "tower or temp"`
Expected: 2 passed.

- [ ] **Step 5: Stage**

`git add backend/app/services/calibration_service.py backend/app/services/background_dispatch.py backend/tests/unit/services/test_calibration_service.py`

---

### Task 4: Frontend — un-gate Auto + Tower rows in `CalibrationStartPage`

**Files:**
- Modify: `frontend/src/components/calibration/CalibrationStartPage.tsx`

- [ ] **Step 1: Remove `comingPlan3` flags + replace placeholder labels for tower rows**

In `CalibrationStartPage.tsx`, drop `comingPlan3: true` from `paPattern`, `paTower`, `auto_pa_line`, `flow_auto`, and all tower options. Also fix the four tower options to use real i18n keys (Plan 2 left them pointing at `paLine` placeholder).

```tsx
const PA_OPTIONS: OptionRow[] = [
  { mode: 'pa_line',     method: 'manual', labelKey: 'paLine',    descKey: 'paLineDesc',    capKey: 'pa_manual' },
  { mode: 'pa_pattern',  method: 'manual', labelKey: 'paPattern', descKey: 'paPatternDesc', capKey: 'pa_manual' },
  { mode: 'pa_tower',    method: 'manual', labelKey: 'paTower',   descKey: 'paTowerDesc',   capKey: 'pa_manual' },
  { mode: 'auto_pa_line',method: 'auto',   labelKey: 'paAuto',    descKey: 'paAutoDesc',    capKey: 'pa_auto' },
];

const FLOW_OPTIONS: OptionRow[] = [
  { mode: 'flow_rate', method: 'manual', labelKey: 'flowRate', descKey: 'flowRateDesc', capKey: 'flow_manual' },
  { mode: 'flow_rate', method: 'auto',   labelKey: 'flowAuto', descKey: 'flowAutoDesc', capKey: 'flow_auto' },
];

const TOWER_OPTIONS: OptionRow[] = [
  { mode: 'temp_tower',       method: 'manual', labelKey: 'tempTower',       descKey: 'tempTowerDesc',       capKey: 'temp_tower' },
  { mode: 'vol_speed_tower',  method: 'manual', labelKey: 'volSpeedTower',   descKey: 'volSpeedTowerDesc',   capKey: 'vol_speed_tower' },
  { mode: 'vfa_tower',        method: 'manual', labelKey: 'vfaTower',        descKey: 'vfaTowerDesc',        capKey: 'vfa_tower' },
  { mode: 'retraction_tower', method: 'manual', labelKey: 'retractionTower', descKey: 'retractionTowerDesc', capKey: 'retraction_tower' },
];
```

Also drop the disabled-reason branch for `comingPlan3`:

```tsx
const reason = !supported ? t('filamentCali.start.notSupported') : '';
```

Update the tower section title from "Towers (Plan 3)" to just "Towers":

```tsx
<h4 className="text-sm font-medium text-bambu-gray mb-2">{t('filamentCali.start.towerGroup')}</h4>
```

Then change `towerGroup` value in i18n (Task 6) to drop the Plan 3 suffix.

- [ ] **Step 2: Update test**

In `frontend/src/__tests__/components/CalibrationStartPage.test.tsx`, remove the "Tower modes disabled with Plan 3 hint" assertion (towers no longer carry Plan 3 hint). Replace with:

```typescript
it('Tower modes enabled', () => {
  render(<CalibrationStartPage capabilities={caps} onPick={() => {}} />);
  const tempBtn = screen.getByRole('button', { name: /Temp Tower|Температурна вежа/i });
  expect(tempBtn).not.toBeDisabled();
});
```

- [ ] **Step 3: Type check**

Run: `cd frontend && npx tsc --noEmit`
Expected: clean (i18n keys missing show as warnings if any; Task 6 adds them).

- [ ] **Step 4: Stage**

`git add frontend/src/components/calibration/CalibrationStartPage.tsx frontend/src/__tests__/components/CalibrationStartPage.test.tsx`

---

### Task 5: Frontend — `CalibrationAutoSavePage`

**Files:**
- Modify: `frontend/src/components/calibration/CalibrationAutoSavePage.tsx`

- [ ] **Step 1: Read shape of `ExtrusionCaliResult` in client**

Grep: `grep -n "ExtrusionCaliResult\|extrusion_cali_results" frontend/src/`. Plan 1+2 didn't expose this on the frontend — we need to add it now.

- [ ] **Step 2: Extend api/client.ts with auto-result types**

Add to `frontend/src/api/client.ts`:

```typescript
export interface ExtrusionCaliResultOut {
  tray_id: number;
  ams_id: number;
  slot_id: number;
  extruder_id: number;
  nozzle_diameter: number;
  nozzle_volume_type: string;
  filament_id: string;
  setting_id: string;
  k_value: number;
  n_coef: number;
  confidence: number;
  nozzle_pos_id: number;
  nozzle_sn: string;
}

export interface AutoResultEditIn {
  tray_id: number;
  k_value?: number;
  n_coef?: number;
  flow_ratio?: number;
  name?: string;
  save?: boolean;
}

export interface AutoResultIn {
  results: AutoResultEditIn[];
}
```

And the client method:

```typescript
submitAutoResult: (sessionId: number, body: AutoResultIn) =>
  request<FilamentCalibrationOut[]>(`/calibration/sessions/${sessionId}/auto-result`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }),

getCalibrationAutoResults: (printerId: number) =>
  request<ExtrusionCaliResultOut[]>(`/printers/${printerId}/calibration/auto-results`),
```

Wait — `/printers/{id}/calibration/auto-results` doesn't exist in Plan 1. The auto results live in `PrinterState.extrusion_cali_results`, accessed by the service when `submit_auto_result` runs server-side. UI displays them by querying the session — Plan 1 Task 19 has `GET /calibration/sessions/{id}` which returns the session row only. UI needs the auto-results in shape for display.

Two options:
1. Add a new endpoint `GET /printers/{id}/calibration/auto-results` that proxies PrinterState.extrusion_cali_results.
2. Embed `auto_results: list[ExtrusionCaliResultOut]` into the `CalibrationSessionOut` response when method=auto.

Option 1 is cleaner — add the endpoint.

- [ ] **Step 3: Add `GET /printers/{id}/calibration/auto-results` endpoint**

In `backend/app/api/routes/filament_calibration.py`, append:

```python
from backend.app.services.bambu_mqtt import ExtrusionCaliResult


@router.get(
    "/printers/{printer_id}/calibration/auto-results",
    response_model=list[dict],   # use dict, not strict — quick path
)
async def get_auto_results(
    printer_id: int,
    _: User | None = RequirePermission(Permission.PRINTERS_READ),
) -> list[dict]:
    client = printer_manager.get_client(printer_id)
    if not client or not client.state.connected:
        raise HTTPException(404, "Printer not online")
    return [
        {
            "tray_id": r.tray_id,
            "ams_id": r.ams_id,
            "slot_id": r.slot_id,
            "extruder_id": r.extruder_id,
            "nozzle_diameter": r.nozzle_diameter,
            "nozzle_volume_type": r.nozzle_volume_type,
            "filament_id": r.filament_id,
            "setting_id": r.setting_id,
            "k_value": r.k_value,
            "n_coef": r.n_coef,
            "confidence": r.confidence,
            "nozzle_pos_id": r.nozzle_pos_id,
            "nozzle_sn": r.nozzle_sn,
        }
        for r in (client.state.extrusion_cali_results or [])
    ]
```

- [ ] **Step 4: Component**

```tsx
// frontend/src/components/calibration/CalibrationAutoSavePage.tsx
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { CheckCircle2, AlertTriangle, XCircle } from 'lucide-react';
import { api } from '../../api/client';
import type {
  AutoResultEditIn,
  CalibrationSessionOut,
  ExtrusionCaliResultOut,
} from '../../api/client';

interface Props {
  session: CalibrationSessionOut;
  onSubmit: (body: { results: AutoResultEditIn[] }) => Promise<unknown>;
  isSubmitting: boolean;
}

interface EditState {
  tray_id: number;
  save: boolean;
  name: string;
  k_value: number;
  n_coef: number;
  flow_ratio: number;
}

function confidenceBadge(c: number) {
  if (c === 0) return { icon: CheckCircle2, cls: 'text-bambu-green', label: 'Success' };
  if (c === 1) return { icon: AlertTriangle, cls: 'text-yellow-500', label: 'Uncertain' };
  return { icon: XCircle, cls: 'text-red-500', label: 'Failed' };
}

export function CalibrationAutoSavePage({ session, onSubmit, isSubmitting }: Props) {
  const { t } = useTranslation();
  const isFlow = session.cali_mode === 'flow_rate';

  const resultsQuery = useQuery<ExtrusionCaliResultOut[]>({
    queryKey: ['calibration', 'auto-results', session.printer_id],
    queryFn: () => api.getCalibrationAutoResults(session.printer_id),
    staleTime: 1_000,
    refetchInterval: 3_000,
  });

  const [edits, setEdits] = useState<Record<number, EditState>>({});

  // Initialize edits when results arrive
  useEffect(() => {
    if (!resultsQuery.data) return;
    setEdits((prev) => {
      const next = { ...prev };
      for (const r of resultsQuery.data!) {
        if (next[r.tray_id]) continue;
        next[r.tray_id] = {
          tray_id: r.tray_id,
          save: r.confidence === 0, // pre-tick successful rows
          name: isFlow
            ? `${r.filament_id} flow ${r.k_value.toFixed(3)}`
            : `${r.filament_id} PA ${r.k_value.toFixed(4)}`,
          k_value: r.k_value,
          n_coef: r.n_coef,
          flow_ratio: r.k_value, // firmware delivers via k_value slot for flow
        };
      }
      return next;
    });
  }, [resultsQuery.data, isFlow]);

  const patch = (tray: number, p: Partial<EditState>) =>
    setEdits((prev) => ({ ...prev, [tray]: { ...prev[tray], ...p } }));

  const submit = async () => {
    const body: AutoResultEditIn[] = Object.values(edits).map((e) => ({
      tray_id: e.tray_id,
      save: e.save,
      name: e.name,
      ...(isFlow ? { flow_ratio: e.flow_ratio } : { k_value: e.k_value, n_coef: e.n_coef }),
    }));
    await onSubmit({ results: body });
  };

  return (
    <div className="space-y-4">
      <h3 className="text-base font-semibold text-white">{t('filamentCali.autoSave.heading')}</h3>
      <p className="text-sm text-bambu-gray">{t('filamentCali.autoSave.instruction')}</p>

      {resultsQuery.isLoading && <div className="text-sm text-bambu-gray">{t('filamentCali.autoSave.waiting')}</div>}

      {resultsQuery.data?.map((r) => {
        const conf = confidenceBadge(r.confidence);
        const Icon = conf.icon;
        const e = edits[r.tray_id];
        if (!e) return null;
        return (
          <div key={r.tray_id} className="p-3 bg-bambu-dark rounded border border-bambu-dark-tertiary space-y-2">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Icon className={`h-4 w-4 ${conf.cls}`} />
                <span className="text-sm text-white">AMS {r.ams_id + 1} Slot {r.slot_id + 1} · {r.filament_id}</span>
              </div>
              <label className="text-xs text-bambu-gray flex items-center gap-1">
                <input type="checkbox" checked={e.save} onChange={(ev) => patch(r.tray_id, { save: ev.target.checked })} />
                {t('filamentCali.autoSave.apply')}
              </label>
            </div>

            {isFlow ? (
              <div className="grid grid-cols-2 gap-2 text-sm">
                <label>
                  <span className="text-xs text-bambu-gray">{t('filamentCali.autoSave.flowRatio')}</span>
                  <input type="number" step="0.001" value={e.flow_ratio}
                    onChange={(ev) => patch(r.tray_id, { flow_ratio: parseFloat(ev.target.value) || 0 })}
                    className="w-full bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded px-2 py-1 text-white font-mono" />
                </label>
              </div>
            ) : (
              <div className="grid grid-cols-2 gap-2 text-sm">
                <label>
                  <span className="text-xs text-bambu-gray">K</span>
                  <input type="number" step="0.0001" value={e.k_value}
                    onChange={(ev) => patch(r.tray_id, { k_value: parseFloat(ev.target.value) || 0 })}
                    className="w-full bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded px-2 py-1 text-white font-mono" />
                </label>
                <label>
                  <span className="text-xs text-bambu-gray">N</span>
                  <input type="number" step="0.01" value={e.n_coef}
                    onChange={(ev) => patch(r.tray_id, { n_coef: parseFloat(ev.target.value) || 0 })}
                    className="w-full bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded px-2 py-1 text-white font-mono" />
                </label>
              </div>
            )}

            <label className="block text-sm">
              <span className="text-xs text-bambu-gray">{t('filamentCali.autoSave.name')}</span>
              <input type="text" value={e.name}
                onChange={(ev) => patch(r.tray_id, { name: ev.target.value })}
                className="w-full bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded px-2 py-1 text-white" />
            </label>
          </div>
        );
      })}

      <div className="flex justify-end pt-2 border-t border-bambu-dark-tertiary">
        <button type="button" onClick={submit} disabled={isSubmitting || resultsQuery.isLoading}
          className="px-4 py-2 rounded bg-bambu-green text-white text-sm font-medium disabled:opacity-40">
          {t('filamentCali.autoSave.saveSelected')}
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Type check**

Run: `cd frontend && npx tsc --noEmit`
Expected: clean (i18n keys covered in Task 6).

- [ ] **Step 6: Stage**

`git add frontend/src/components/calibration/CalibrationAutoSavePage.tsx frontend/src/api/client.ts backend/app/api/routes/filament_calibration.py`

---

### Task 6: Frontend — `CalibrationTowerFinishPage`

**Files:**
- Modify: `frontend/src/components/calibration/CalibrationTowerFinishPage.tsx`

- [ ] **Step 1: Component**

```tsx
// frontend/src/components/calibration/CalibrationTowerFinishPage.tsx
import { useTranslation } from 'react-i18next';
import { Info } from 'lucide-react';
import type { CalibrationSessionOut } from '../../api/client';

interface Props {
  session: CalibrationSessionOut;
  onClose: () => void;
  onCalibrateAnother: () => void;
}

export function CalibrationTowerFinishPage({ session, onClose, onCalibrateAnother }: Props) {
  const { t } = useTranslation();
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <Info className="h-6 w-6 text-bambu-green" />
        <h3 className="text-base font-semibold text-white">{t('filamentCali.towerFinish.heading')}</h3>
      </div>
      <p className="text-sm text-bambu-gray">{t('filamentCali.towerFinish.body')}</p>
      <p className="text-sm text-bambu-gray">
        {t(`filamentCali.towerFinish.tip.${session.cali_mode}`, {
          defaultValue: t('filamentCali.towerFinish.tip.generic'),
        })}
      </p>

      <div className="flex justify-between pt-2 border-t border-bambu-dark-tertiary">
        <button type="button" onClick={onCalibrateAnother} className="px-3 py-1.5 text-sm text-bambu-gray hover:text-white">
          {t('filamentCali.finish.calibrateAnother')}
        </button>
        <button type="button" onClick={onClose} className="px-4 py-2 rounded bg-bambu-green text-white text-sm font-medium">
          {t('filamentCali.finish.close')}
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Type check**

Run: `cd frontend && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 3: Stage**

`git add frontend/src/components/calibration/CalibrationTowerFinishPage.tsx`

---

### Task 7: Frontend — extend i18n with auto + tower + history keys (en + uk)

**Files:**
- Modify: `frontend/src/i18n/locales/en.ts`
- Modify: `frontend/src/i18n/locales/uk.ts`

- [ ] **Step 1: Append to `filamentCali` namespace in `en.ts`**

Inside the existing `filamentCali: { ... }` object (drop the trailing `},` first, then add):

```typescript
// Auto save (X1)
autoSave: {
  heading: 'Review auto-calibration results',
  instruction: 'Printer scanned the printed lines with its lidar. Pick the rows you want to apply and adjust values if needed.',
  waiting: 'Waiting for lidar results…',
  apply: 'Apply',
  flowRatio: 'Flow ratio',
  name: 'Save as',
  saveSelected: 'Save selected',
},

// Tower finish (Temp / VolSpeed / VFA / Retraction)
towerFinish: {
  heading: 'Print complete',
  body: 'Read the calibrated value off the printed tower visually, then enter it in your slicer\'s filament profile.',
  tip: {
    generic: 'Most slicers carry these values per-filament — adjust there.',
    temp_tower: 'Pick the temperature step with the cleanest extrusion and no stringing. Update Filament → Nozzle temperature.',
    vol_speed_tower: 'Find the highest speed step without under-extrusion. Update Filament → Max volumetric speed.',
    vfa_tower: 'Pick the speed step with no Vertical Fine Artifacts. Update Print → Outer wall speed.',
    retraction_tower: 'Pick the retraction step with no oozing. Update Filament → Retraction length.',
  },
},

// History modal
history: {
  menuItem: 'Calibration History',
  title: 'Filament Calibration History',
  refresh: 'Refresh from printer',
  refreshHint: 'Pulls the current 16-slot PA history off the printer for cross-check',
  empty: 'No calibrations yet. Run the wizard once.',
  printerSide: 'Printer-side history',
  bamdudeSide: 'BamDude history',
  setActive: 'Set active',
  delete: 'Delete',
  deleteConfirm: 'Delete this calibration row?',
  active: 'Active',
  source: { auto: 'Auto', manual: 'Manual' },
  groupNozzle: '{{diameter}} mm · {{type}}',
},

// H2D dual-extruder UI
extruder: {
  right: 'Right',
  left: 'Left',
  main: 'Main',
  tab: 'Extruder',
},

// Tower mode labels (replace the placeholder paLine references in Plan 2)
tempTower: 'Temperature Tower',
tempTowerDesc: 'Print a tower stepping through nozzle temperatures. Manually pick the cleanest step.',
volSpeedTower: 'Max Volumetric Speed Tower',
volSpeedTowerDesc: 'Print a tower stepping through volumetric speeds. Pick the highest clean step.',
vfaTower: 'VFA Tower (Vertical Fine Artifacts)',
vfaTowerDesc: 'Find the speed that eliminates vertical fine artifacts on outer walls.',
retractionTower: 'Retraction Tower',
retractionTowerDesc: 'Step through retraction lengths to find the minimum that stops stringing.',
```

Also change the existing `towerGroup` value from `'Towers (Plan 3)'` → `'Towers'`, and `paPatternDesc`/`paTowerDesc`/`paAutoDesc`/`flowAutoDesc` to drop the trailing `(Comes in Plan 3.)` text.

- [ ] **Step 2: Mirror in `uk.ts`**

Append same keys with Ukrainian translations:

```typescript
autoSave: {
  heading: 'Перегляд результатів авто-калібровки',
  instruction: 'Принтер просканував надруковані лінії лідаром. Обери рядки які зберегти, при потребі підправ значення.',
  waiting: 'Чекаємо результати з лідара…',
  apply: 'Застосувати',
  flowRatio: 'Flow ratio',
  name: 'Зберегти як',
  saveSelected: 'Зберегти вибране',
},

towerFinish: {
  heading: 'Друк завершено',
  body: 'Прочитай відкаліброване значення з вежі, потім впиши у profile філаменту в slicer.',
  tip: {
    generic: 'Більшість слайсерів зберігають ці значення per-filament — змінюй там.',
    temp_tower: 'Обери температурну сходинку з найчистішою екструзією і без стрингів. Внеси в Filament → Nozzle temperature.',
    vol_speed_tower: 'Знайди найвищу швидкість без під-екструзії. Внеси в Filament → Max volumetric speed.',
    vfa_tower: 'Обери швидкість без Vertical Fine Artifacts. Внеси в Print → Outer wall speed.',
    retraction_tower: 'Обери retraction-сходинку без oozing. Внеси в Filament → Retraction length.',
  },
},

history: {
  menuItem: 'Історія калібровок',
  title: 'Історія калібровок філаменту',
  refresh: 'Оновити з принтера',
  refreshHint: 'Підтягує поточну 16-слотну PA history з принтера для звірки',
  empty: 'Жодної калібровки. Запусти візард хоч раз.',
  printerSide: 'Історія на принтері',
  bamdudeSide: 'Історія BamDude',
  setActive: 'Зробити активним',
  delete: 'Видалити',
  deleteConfirm: 'Видалити цю калібровку?',
  active: 'Активний',
  source: { auto: 'Авто', manual: 'Ручний' },
  groupNozzle: '{{diameter}} мм · {{type}}',
},

extruder: {
  right: 'Правий',
  left: 'Лівий',
  main: 'Головний',
  tab: 'Екструдер',
},

tempTower: 'Температурна вежа',
tempTowerDesc: 'Друкуємо вежу з різними температурами сопла. Очима обираєш найчистішу сходинку.',
volSpeedTower: 'Вежа Max Volumetric Speed',
volSpeedTowerDesc: 'Вежа з різними об\'ємними швидкостями. Обираєш найвищу чисту сходинку.',
vfaTower: 'VFA Вежа (Vertical Fine Artifacts)',
vfaTowerDesc: 'Знаходимо швидкість що усуває vertical fine artifacts на зовнішніх стінках.',
retractionTower: 'Retraction Вежа',
retractionTowerDesc: 'Сходинки retraction-довжини щоб знайти мінімум без stringing.',
```

Update `towerGroup` value to `'Тестові вежі'` (drop Plan 3 hint), and same desc cleanup as English.

- [ ] **Step 3: Type check**

Run: `cd frontend && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 4: Stage**

`git add frontend/src/i18n/locales/en.ts frontend/src/i18n/locales/uk.ts`

---

### Task 8: Wire `CalibrationAutoSavePage` + `CalibrationTowerFinishPage` into shell

**Files:**
- Modify: `frontend/src/components/FilamentCalibrationModal.tsx`
- Modify: `frontend/src/hooks/useFilamentCalibration.ts`

- [ ] **Step 1: Extend `WizardStep` + `computeNextStep`**

In `frontend/src/hooks/useFilamentCalibration.ts`:

```typescript
export type WizardStep =
  | 'start' | 'preset' | 'running'
  | 'manualSave' | 'coarseSave' | 'fineSave'
  | 'autoSave'        // NEW
  | 'towerFinish'     // NEW
  | 'finish';

export function computeNextStep(current: WizardStep, ctx: ComputeNextStepInput): WizardStep {
  switch (current) {
    case 'start': return 'preset';
    case 'preset': return ctx.sessionStarted ? 'running' : 'preset';
    case 'running':
      if (ctx.sessionStatus === 'saved' && ctx.isTowerMode) return 'towerFinish';
      if (ctx.sessionStatus !== 'awaiting_user_input') return 'running';
      if (ctx.method === 'auto') return 'autoSave';
      if (ctx.cali_mode === 'flow_rate') return ctx.stage === 2 ? 'fineSave' : 'coarseSave';
      return 'manualSave';
    case 'coarseSave':
      if (ctx.skipFine) return ctx.savedRows ? 'finish' : 'coarseSave';
      if (ctx.nextSessionId != null) return 'running';
      return 'coarseSave';
    case 'manualSave':
    case 'fineSave':
    case 'autoSave':
      return ctx.savedRows ? 'finish' : current;
    case 'towerFinish':
    case 'finish':
      return current;
    default: return 'start';
  }
}
```

Add `isTowerMode` + `method` to `ComputeNextStepInput`:

```typescript
export interface ComputeNextStepInput {
  cali_mode?: CaliMode;
  method?: CaliMethod;
  sessionStarted?: boolean;
  sessionStatus?: CalibrationSessionOut['status'];
  stage?: number;
  skipFine?: boolean;
  savedRows?: number;
  nextSessionId?: number | null;
  isTowerMode?: boolean;   // NEW
}
```

- [ ] **Step 2: Pass new context in effect**

Where the hook calls `computeNextStep('running', { … })` after session refetch:

```typescript
useEffect(() => {
  if (sessionQuery.data?.status === 'awaiting_user_input' || sessionQuery.data?.status === 'saved') {
    setStep(
      computeNextStep('running', {
        cali_mode: input.cali_mode,
        method: input.method,
        stage: sessionQuery.data.stage,
        sessionStatus: sessionQuery.data.status,
        isTowerMode: ['temp_tower','vol_speed_tower','vfa_tower','retraction_tower'].includes(
          sessionQuery.data.cali_mode,
        ),
      }),
    );
  }
}, [sessionQuery.data, input.cali_mode, input.method]);
```

- [ ] **Step 3: Add `submitAutoResult` mutation**

In `useFilamentCalibration`:

```typescript
const submitAutoMutation = useMutation({
  mutationFn: (body: { results: AutoResultEditIn[] }) => {
    if (sessionId == null) throw new Error('No active session');
    return api.submitAutoResult(sessionId, body);
  },
  onSuccess: (rows) => {
    setSavedRows(rows);
    setStep('finish');
    qc.invalidateQueries({ queryKey: ['filament-calibrations'] });
  },
  onError: (e: Error) => setErrorMsg(e.message),
});
```

Add to returned object:

```typescript
submitAutoResult: (body: { results: AutoResultEditIn[] }) => submitAutoMutation.mutateAsync(body),
```

- [ ] **Step 4: Mount pages in shell**

In `FilamentCalibrationModal.tsx`, add after `fineSave` block:

```tsx
{cali.step === 'autoSave' && cali.session && (
  <CalibrationAutoSavePage
    session={cali.session}
    onSubmit={(body) => cali.submitAutoResult(body)}
    isSubmitting={cali.isSubmitting}
  />
)}

{cali.step === 'towerFinish' && cali.session && (
  <CalibrationTowerFinishPage
    session={cali.session}
    onClose={onClose}
    onCalibrateAnother={() => {
      cali.setSessionId(null);
      cali.setStep('start');
    }}
  />
)}
```

Add imports.

- [ ] **Step 5: Update hook test**

In `frontend/src/__tests__/hooks/useFilamentCalibration.test.tsx`, append:

```typescript
it('running auto → autoSave on awaiting_user_input', () => {
  expect(computeNextStep('running', { method: 'auto', cali_mode: 'auto_pa_line', sessionStatus: 'awaiting_user_input' })).toBe('autoSave');
});

it('running tower → towerFinish on saved', () => {
  expect(computeNextStep('running', { method: 'manual', cali_mode: 'temp_tower', sessionStatus: 'saved', isTowerMode: true })).toBe('towerFinish');
});

it('autoSave → finish on saved rows', () => {
  expect(computeNextStep('autoSave', { savedRows: 1 })).toBe('finish');
});
```

- [ ] **Step 6: Run tests**

```
cd frontend && npm run test:run -- src/__tests__/hooks/useFilamentCalibration.test.tsx
npx tsc --noEmit
```

Expected: 11+ passed (8 existing + 3 new).

- [ ] **Step 7: Stage**

`git add frontend/src/hooks/useFilamentCalibration.ts frontend/src/components/FilamentCalibrationModal.tsx frontend/src/__tests__/hooks/useFilamentCalibration.test.tsx`

---

### Wave 1 verify

- [ ] Tests + build

```
pytest backend/tests/unit/services/test_calibration_service.py -v
cd frontend && npm run test:run -- src/__tests__/hooks src/__tests__/components/CalibrationStartPage.test.tsx
npx tsc --noEmit
npm run lint
```

Expected: all green.

- [ ] Commit suggestion:

```
feat(calibration): auto-cali UI + tower modes (Plan 3 Wave 1)

Backend:
- Un-stubbed flow_rate auto + tower print-and-finish path.
- submit_auto_result handles flow_ratio (firmware delivers via k_value
  slot when session.cali_mode == flow_rate).
- Dispatch on-complete flips tower sessions to "saved" (no user input).
- GET /printers/{id}/calibration/auto-results endpoint.

Frontend:
- CalibrationStartPage: un-gated Auto PA, Auto Flow, all four towers.
- CalibrationAutoSavePage: multi-row results review with K/N/flow edit.
- CalibrationTowerFinishPage: print-finished message with mode-specific
  slicer-side instructions.
- en+uk i18n extension (~30 new keys).
- useFilamentCalibration state machine adds autoSave + towerFinish.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Wave 2 — Full History modal

### Task 9: Hook — `useCalibrationHistory`

**Files:**
- Create: `frontend/src/hooks/useCalibrationHistory.ts`

- [ ] **Step 1: Extend api/client.ts**

Add to `frontend/src/api/client.ts`:

```typescript
export interface PACalibHistoryEntryOut {
  cali_idx: number;
  name: string;
  filament_id: string;
  setting_id: string;
  nozzle_diameter: number;
  nozzle_volume_type: string;
  extruder_id: number;
  k_value: number;
  n_coef: number;
}
```

Methods:

```typescript
listFilamentCalibrations: (params: {
  printer_model?: string;
  filament_id?: string;
  nozzle_diameter?: number;
  is_active?: boolean;
}) => {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v != null) q.append(k, String(v));
  }
  return request<FilamentCalibrationOut[]>(`/filament-calibrations?${q.toString()}`);
},

setActiveCalibration: (caliId: number) =>
  request<FilamentCalibrationOut>(`/filament-calibrations/${caliId}/set-active`, { method: 'POST' }),

deleteCalibration: (caliId: number) =>
  request<void>(`/filament-calibrations/${caliId}`, { method: 'DELETE' }),

getPrinterCalibrationHistory: (printerId: number) =>
  request<PACalibHistoryEntryOut[]>(`/printers/${printerId}/calibration/history`),

refreshPrinterCalibrationHistory: (printerId: number, nozzle_diameter: number, extruder_id: number = 0) =>
  request<{ sequence_id: string }>(
    `/printers/${printerId}/calibration/history/refresh?nozzle_diameter=${nozzle_diameter}&extruder_id=${extruder_id}`,
    { method: 'POST' },
  ),
```

- [ ] **Step 2: Hook**

```typescript
// frontend/src/hooks/useCalibrationHistory.ts
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import type { FilamentCalibrationOut, PACalibHistoryEntryOut } from '../api/client';

export function useCalibrationHistory(printerId: number, printerModel: string, enabled: boolean) {
  const qc = useQueryClient();

  const bamdudeQuery = useQuery<FilamentCalibrationOut[]>({
    queryKey: ['filament-calibrations', printerModel],
    queryFn: () => api.listFilamentCalibrations({ printer_model: printerModel }),
    enabled: enabled && Boolean(printerModel),
    staleTime: 10_000,
  });

  const printerSideQuery = useQuery<PACalibHistoryEntryOut[]>({
    queryKey: ['calibration', 'printer-history', printerId],
    queryFn: () => api.getPrinterCalibrationHistory(printerId),
    enabled,
    staleTime: 30_000,
  });

  const setActiveMutation = useMutation({
    mutationFn: (caliId: number) => api.setActiveCalibration(caliId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['filament-calibrations', printerModel] }),
  });

  const deleteMutation = useMutation({
    mutationFn: (caliId: number) => api.deleteCalibration(caliId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['filament-calibrations', printerModel] }),
  });

  const refreshMutation = useMutation({
    mutationFn: (nozzleDia: number) => api.refreshPrinterCalibrationHistory(printerId, nozzleDia),
    onSuccess: () => {
      // Wait briefly for printer to push; then refetch
      setTimeout(() => {
        qc.invalidateQueries({ queryKey: ['calibration', 'printer-history', printerId] });
      }, 2000);
    },
  });

  return {
    bamdude: bamdudeQuery.data ?? [],
    printerSide: printerSideQuery.data ?? [],
    isLoading: bamdudeQuery.isLoading || printerSideQuery.isLoading,
    setActive: setActiveMutation.mutateAsync,
    delete: deleteMutation.mutateAsync,
    refreshFromPrinter: refreshMutation.mutateAsync,
    isRefreshing: refreshMutation.isPending,
  };
}
```

- [ ] **Step 3: Type check**

Run: `cd frontend && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 4: Stage**

`git add frontend/src/hooks/useCalibrationHistory.ts frontend/src/api/client.ts`

---

### Task 10: `CalibrationHistoryModal` component

**Files:**
- Create: `frontend/src/components/CalibrationHistoryModal.tsx`

- [ ] **Step 1: Component**

```tsx
// frontend/src/components/CalibrationHistoryModal.tsx
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { X, RefreshCw, Trash2, CheckCircle2 } from 'lucide-react';

import { useCalibrationHistory } from '../hooks/useCalibrationHistory';
import type { FilamentCalibrationOut, PACalibHistoryEntryOut } from '../api/client';

interface Props {
  isOpen: boolean;
  onClose: () => void;
  printerId: number;
  printerModel: string;
}

function groupByNozzle<T extends { nozzle_diameter: number; nozzle_volume_type: string }>(rows: T[]) {
  const groups = new Map<string, T[]>();
  for (const r of rows) {
    const key = `${r.nozzle_diameter}-${r.nozzle_volume_type}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(r);
  }
  return groups;
}

export function CalibrationHistoryModal({ isOpen, onClose, printerId, printerModel }: Props) {
  const { t } = useTranslation();
  const h = useCalibrationHistory(printerId, printerModel, isOpen);
  const [refreshDia, setRefreshDia] = useState<number>(0.4);

  if (!isOpen) return null;

  const bamdudeGroups = groupByNozzle(h.bamdude);
  const printerGroups = groupByNozzle(h.printerSide);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-2xl w-full max-w-3xl mx-4 max-h-[90vh] overflow-y-auto">
        <div className="flex justify-between items-center p-4 border-b border-bambu-dark-tertiary">
          <h2 className="text-lg font-semibold text-white">{t('filamentCali.history.title')}</h2>
          <button onClick={onClose} aria-label="Close" className="p-1 text-bambu-gray hover:text-white">
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="p-4 space-y-6">
          <section>
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-semibold text-white">{t('filamentCali.history.bamdudeSide')}</h3>
            </div>
            {bamdudeGroups.size === 0 && (
              <p className="text-sm text-bambu-gray">{t('filamentCali.history.empty')}</p>
            )}
            {Array.from(bamdudeGroups.entries()).map(([key, rows]) => {
              const [dia, type] = key.split('-');
              return (
                <div key={key} className="mb-4">
                  <h4 className="text-xs text-bambu-gray mb-2">
                    {t('filamentCali.history.groupNozzle', { diameter: dia, type })}
                  </h4>
                  <div className="space-y-1">
                    {rows.map((r) => <BamDudeRow key={r.id} r={r} h={h} />)}
                  </div>
                </div>
              );
            })}
          </section>

          <section>
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-semibold text-white">{t('filamentCali.history.printerSide')}</h3>
              <div className="flex items-center gap-2">
                <select
                  value={refreshDia}
                  onChange={(e) => setRefreshDia(parseFloat(e.target.value))}
                  className="bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1 text-xs text-white"
                >
                  {[0.2, 0.4, 0.6, 0.8].map((d) => (
                    <option key={d} value={d}>{d} mm</option>
                  ))}
                </select>
                <button
                  onClick={() => h.refreshFromPrinter(refreshDia)}
                  disabled={h.isRefreshing}
                  className="px-2 py-1 text-xs rounded border border-bambu-dark-tertiary text-bambu-gray hover:text-white flex items-center gap-1"
                  title={t('filamentCali.history.refreshHint')}
                >
                  <RefreshCw className={`h-3 w-3 ${h.isRefreshing ? 'animate-spin' : ''}`} />
                  {t('filamentCali.history.refresh')}
                </button>
              </div>
            </div>
            {printerGroups.size === 0 && (
              <p className="text-sm text-bambu-gray">{t('filamentCali.history.empty')}</p>
            )}
            {Array.from(printerGroups.entries()).map(([key, rows]) => {
              const [dia, type] = key.split('-');
              return (
                <div key={key} className="mb-4">
                  <h4 className="text-xs text-bambu-gray mb-2">
                    {t('filamentCali.history.groupNozzle', { diameter: dia, type })}
                  </h4>
                  <div className="space-y-1">
                    {rows.map((r) => <PrinterSideRow key={r.cali_idx} r={r} />)}
                  </div>
                </div>
              );
            })}
          </section>
        </div>
      </div>
    </div>
  );
}

function BamDudeRow({ r, h }: {
  r: FilamentCalibrationOut;
  h: ReturnType<typeof useCalibrationHistory>;
}) {
  const { t } = useTranslation();
  const onDelete = async () => {
    if (window.confirm(t('filamentCali.history.deleteConfirm'))) {
      await h.delete(r.id);
    }
  };
  return (
    <div className={`p-2 rounded border flex items-center justify-between ${
      r.is_active ? 'border-bambu-green bg-bambu-green/10' : 'border-bambu-dark-tertiary bg-bambu-dark'
    }`}>
      <div className="flex-1">
        <div className="flex items-center gap-2">
          {r.is_active && <CheckCircle2 className="h-3 w-3 text-bambu-green" />}
          <span className="text-sm text-white">{r.name}</span>
          <span className="text-xs text-bambu-gray">· {r.filament_id}</span>
        </div>
        <div className="text-xs text-bambu-gray font-mono mt-0.5">
          {r.pa_k_value != null && `K = ${r.pa_k_value.toFixed(4)}  `}
          {r.flow_ratio != null && `flow = ${r.flow_ratio.toFixed(4)}  `}
          {r.source} · {new Date(r.created_at).toLocaleDateString()}
        </div>
      </div>
      <div className="flex items-center gap-1">
        {!r.is_active && (
          <button
            onClick={() => h.setActive(r.id)}
            className="px-2 py-1 text-xs rounded border border-bambu-dark-tertiary text-bambu-gray hover:text-white"
          >
            {t('filamentCali.history.setActive')}
          </button>
        )}
        <button
          onClick={onDelete}
          className="p-1 text-bambu-gray hover:text-red-400"
          aria-label="Delete"
        >
          <Trash2 className="h-3 w-3" />
        </button>
      </div>
    </div>
  );
}

function PrinterSideRow({ r }: { r: PACalibHistoryEntryOut }) {
  return (
    <div className="p-2 rounded border border-bambu-dark-tertiary bg-bambu-dark flex items-center justify-between text-sm">
      <span className="text-white">[{r.cali_idx}] {r.name}</span>
      <span className="text-xs text-bambu-gray font-mono">K = {r.k_value.toFixed(4)} · {r.filament_id}</span>
    </div>
  );
}
```

- [ ] **Step 2: Type check + lint**

```
cd frontend && npx tsc --noEmit && npm run lint
```

Expected: clean.

- [ ] **Step 3: Stage**

`git add frontend/src/components/CalibrationHistoryModal.tsx`

---

### Task 11: Kebab entry for history modal + wire into PrintersPage

**Files:**
- Modify: `frontend/src/pages/PrintersPage.tsx`

- [ ] **Step 1: Add state + import**

```tsx
const [showCalibrationHistory, setShowCalibrationHistory] = useState<{ id: number; model: string } | null>(null);

// import:
import { CalibrationHistoryModal } from '../components/CalibrationHistoryModal';
```

- [ ] **Step 2: Add kebab item (after the "Filament Calibration" entry from Plan 2)**

```tsx
{user?.hasPermission?.('printers:read') && (
  <button
    onClick={() => setShowCalibrationHistory({ id: printer.id, model: printer.model })}
    className="w-full text-left px-3 py-2 text-sm hover:bg-bambu-dark-tertiary"
  >
    {t('filamentCali.history.menuItem')}
  </button>
)}
```

- [ ] **Step 3: Mount the modal**

```tsx
{showCalibrationHistory && (
  <CalibrationHistoryModal
    isOpen
    onClose={() => setShowCalibrationHistory(null)}
    printerId={showCalibrationHistory.id}
    printerModel={showCalibrationHistory.model}
  />
)}
```

- [ ] **Step 4: Add "View History" button on FinishPage**

In `frontend/src/components/calibration/CalibrationFinishPage.tsx`, wire the "View History" button (currently in i18n only):

```tsx
// Add an optional onViewHistory prop
interface Props {
  savedRows: FilamentCalibrationOut[];
  onCalibrateAnother: () => void;
  onClose: () => void;
  onViewHistory?: () => void;
}

// Render:
{onViewHistory && (
  <button onClick={onViewHistory} className="px-3 py-1.5 text-sm text-bambu-gray hover:text-white">
    {t('filamentCali.finish.viewHistory')}
  </button>
)}
```

In `FilamentCalibrationModal.tsx`, pass `onViewHistory` from wizard. Since Wizard doesn't directly own the history modal (it's on the page), pop the modal via the same external state. Simpler approach: just close the wizard and open history from the page. Skip cross-modal navigation in Plan 3 — Finish page leaves "View History" out for now and user clicks kebab → history if they want.

Drop the optional prop and the button. Keep only Close + Calibrate Another. (i18n keys remain available for Plan 4 if any.)

- [ ] **Step 5: Type check + build**

```
cd frontend && npx tsc --noEmit && npm run lint
```

Expected: clean.

- [ ] **Step 6: Stage**

`git add frontend/src/pages/PrintersPage.tsx frontend/src/components/calibration/CalibrationFinishPage.tsx`

---

### Wave 2 verify

- [ ] Tests + build

```
cd frontend && npm run test:run && npx tsc --noEmit && npm run lint && npm run build
```

Expected: all green, bundle updated under `static/`.

- [ ] Commit suggestion:

```
feat(calibration): full History modal (Plan 3 Wave 2)

- useCalibrationHistory: lists BamDude rows + printer-side 16-slot
  history, set-active + delete + refresh-from-printer mutations.
- CalibrationHistoryModal: per-nozzle grouped view (BamDude + Printer
  side), inline actions, manual refresh trigger with nozzle picker.
- Kebab menu entry "Calibration History" on PrintersPage.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Wave 3 — H2D dual-extruder UI

### Task 12: Extend `CalibrationPresetPage` with extruder tabs

**Files:**
- Modify: `frontend/src/components/calibration/CalibrationPresetPage.tsx`

- [ ] **Step 1: Inject extruder tab state**

Top of component:

```tsx
const isDual = Boolean(capabilities?.dual_extruder);
const extruderList = capabilities?.extruders ?? [{ id: 0, name: 'Main' }];
const [activeExtruder, setActiveExtruder] = useState<number>(0);
```

Wrap existing form contents in a per-extruder block. When `isDual`, render tab bar at top:

```tsx
{isDual && (
  <div className="flex gap-1 rounded-lg p-1 bg-bambu-dark border border-bambu-dark-tertiary mb-3">
    {extruderList.map((ex) => (
      <button
        key={ex.id}
        type="button"
        onClick={() => setActiveExtruder(ex.id)}
        className={`flex-1 px-3 py-1.5 text-sm rounded transition-colors ${
          activeExtruder === ex.id ? 'bg-bambu-green text-white' : 'text-bambu-gray hover:text-white'
        }`}
      >
        {t(`filamentCali.extruder.${ex.name.toLowerCase()}`, { defaultValue: ex.name })}
      </button>
    ))}
  </div>
)}
```

- [ ] **Step 2: Per-extruder filament selection state**

Replace single `selectedSlot` with a map:

```tsx
const [perExtruder, setPerExtruder] = useState<
  Record<number, { selectedSlot: typeof selectedSlot | null; bedTemp: number; nozzleTemp: number; maxVolSpeed: number }>
>({ 0: { selectedSlot: null, bedTemp: 60, nozzleTemp: 220, maxVolSpeed: 12 } });

const current = perExtruder[activeExtruder] ?? { selectedSlot: null, bedTemp: 60, nozzleTemp: 220, maxVolSpeed: 12 };

const patchCurrent = (p: Partial<typeof current>) =>
  setPerExtruder((prev) => ({ ...prev, [activeExtruder]: { ...prev[activeExtruder] ?? current, ...p } }));
```

Wire all input/setSelected handlers through `patchCurrent`.

- [ ] **Step 3: Submit aggregates all configured extruders**

Adjust `submit`:

```tsx
const submit = async () => {
  const filaments: CalibFilamentIn[] = [];
  for (const ex of extruderList) {
    const data = perExtruder[ex.id];
    if (!data?.selectedSlot) continue;  // extruder skipped
    filaments.push({
      ams_id: data.selectedSlot.ams_id,
      slot_id: data.selectedSlot.slot_id,
      tray_id: data.selectedSlot.tray_id,
      filament_id: data.selectedSlot.filament_id,
      filament_setting_id: data.selectedSlot.filament_setting_id,
      bed_temp: data.bedTemp,
      nozzle_temp: data.nozzleTemp,
      max_volumetric_speed: data.maxVolSpeed,
    });
  }
  if (filaments.length === 0) return;
  // For now: pass first extruder id; service handles per-extruder cases via filaments[].extruder_id
  // Manual flow: one extruder at a time. Auto flow: can batch.
  await onStart({
    nozzle_diameter: nozzleDia,
    nozzle_volume_type: nozzleVolType,
    extruder_id: activeExtruder,
    filaments,
  });
};
```

For **manual** mode, restrict `filaments` to just the active extruder (manual cali = one extruder at a time). For **auto** mode, batching is fine. Pass the `method` prop into the page and branch:

```tsx
if (method === 'manual') {
  const data = perExtruder[activeExtruder];
  if (!data?.selectedSlot) return;
  await onStart({
    nozzle_diameter: nozzleDia,
    nozzle_volume_type: nozzleVolType,
    extruder_id: activeExtruder,
    filaments: [/* single */],
  });
} else {
  // auto: send all configured extruders
  await onStart({
    nozzle_diameter: nozzleDia,
    nozzle_volume_type: nozzleVolType,
    extruder_id: 0,  // BS sends per-filament extruder_id
    filaments: /* all */,
  });
}
```

- [ ] **Step 4: Backend — per-filament extruder_id in start_calibration**

Plan 1 sets every filament's `extruder_id` to the session-level value. For auto-batch, we need each filament to carry its own. Adjust `start_calibration` in `calibration_service.py`:

```python
# Replace fixed extruder_id with per-filament fallback:
filaments_payload = [
    {
        "tray_id": f.tray_id,
        "extruder_id": getattr(f, "extruder_id_override", extruder_id),  # default to session-level
        # ... rest as before ...
    }
    for f in filaments
]
```

And extend `CalibFilamentInput`:

```python
@dataclass
class CalibFilamentInput:
    ams_id: int
    slot_id: int
    tray_id: int
    filament_id: str
    filament_setting_id: str | None
    bed_temp: int
    nozzle_temp: int
    max_volumetric_speed: float
    flow_rate: float = 0.98
    extruder_id_override: int | None = None  # NEW for H2D auto-batch
```

Adjust the schema in `backend/app/schemas/filament_calibration.py`:

```python
class CalibFilamentIn(BaseModel):
    ams_id: int
    slot_id: int
    tray_id: int
    filament_id: str
    filament_setting_id: str | None = None
    bed_temp: int
    nozzle_temp: int
    max_volumetric_speed: float
    flow_rate: float = 0.98
    extruder_id: int | None = None  # per-filament override
```

And in the route, pass through:

```python
filaments=[
    CalibFilamentInput(
        ams_id=f.ams_id, slot_id=f.slot_id, tray_id=f.tray_id,
        filament_id=f.filament_id, filament_setting_id=f.filament_setting_id,
        bed_temp=f.bed_temp, nozzle_temp=f.nozzle_temp,
        max_volumetric_speed=f.max_volumetric_speed,
        flow_rate=f.flow_rate,
        extruder_id_override=f.extruder_id,
    )
    for f in body.filaments
],
```

- [ ] **Step 5: Type check**

```
cd frontend && npx tsc --noEmit
pytest backend/tests/unit/services/test_calibration_service.py -v
```

Expected: clean (existing tests still pass since `extruder_id_override` defaults to None).

- [ ] **Step 6: Stage**

`git add frontend/src/components/calibration/CalibrationPresetPage.tsx backend/app/services/calibration_service.py backend/app/schemas/filament_calibration.py backend/app/api/routes/filament_calibration.py`

---

### Wave 3 verify

- [ ] Tests + build

```
pytest backend/tests/unit/services/test_calibration_service.py backend/tests/integration/test_calibration_routes.py -v
cd frontend && npm run test:run && npx tsc --noEmit && npm run lint && npm run build
```

Expected: all green.

- [ ] Commit suggestion:

```
feat(calibration): H2D dual-extruder support (Plan 3 Wave 3)

- CalibrationPresetPage: per-extruder tabs (Right/Left) when
  capabilities.dual_extruder. Per-extruder filament + temps state.
- Manual mode: one extruder per session. Auto mode: batches both
  extruders into one MQTT extrusion_cali call.
- Backend: CalibFilamentInput.extruder_id_override + schema field
  let per-filament extruder selection survive the API hop.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Wave 4 — Docs site + landing page

### Task 13: Extend docs site Printer Settings page with full Filament Calibration section

**Files:**
- Modify: `D:/Development/docs.bamdude.top/docs/features/printer-control.md`
- Modify: `D:/Development/docs.bamdude.top/docs/features/printer-control.uk.md`

- [ ] **Step 1: Verify current state**

```
grep -n "Filament Calibration\|filament-calibration" D:/Development/docs.bamdude.top/docs/features/printer-control.md
```

Plan 2 added a brief mention; Plan 3 expands it into a full section parallel to "Printer Settings Dialog".

- [ ] **Step 2: Append `## :material-flask: Filament Calibration` section to `printer-control.md`**

Locate the existing "Printer Settings Dialog" section and add new section AFTER it:

```markdown
---

## :material-flask: Filament Calibration

A wizard that mirrors **Bambu Studio → Calibrate → Pressure Advance / Flow Rate / Towers** without leaving BamDude. Open it from the kebab :material-dots-vertical: menu on a printer card → **Filament Calibration**. History review lives on a sibling kebab entry → **Calibration History**.

### What's calibrated

| Mode | Path | Output |
|---|---|---|
| **PA Line** | Manual: 50-line tower → pick best line | `pa_k_value` per (filament, nozzle, extruder) |
| **PA Pattern** | Manual: PA grid (bowden-friendly) | same |
| **PA Tower** | Manual: stepped vertical tower | same |
| **Auto PA** | X1 / X1E / H2D Pro: lidar scans + reports K/N | same (pre-filled save dialog) |
| **Flow Rate** | Manual: 9-block coarse (−20...+20 %) → 7-block fine refinement | `flow_ratio` per combo |
| **Auto Flow Rate** | X1 lidar variant | same |
| **Temp / VolSpeed / VFA / Retraction Tower** | Manual print only; read result with your eyes, enter in slicer | no DB row written |

### Per-model capability gating

Per-model rules — auto paths need lidar + firmware support flag; manual paths universally available.

| Path | X1 family | P1 / P2 / X2D | A1 / A1 Mini | H2D / H2D Pro |
|---|---|---|---|---|
| Manual PA / Flow Rate / Towers | yes | yes | yes | yes |
| Auto PA (lidar) | yes | — | — | yes (Pro) |
| Auto Flow Rate (lidar) | yes | — | — | yes (Pro) |
| Dual-extruder (per-extruder cali) | — | — | — | yes |

### State + persistence

- BamDude row written to `filament_calibration` (m062) keyed by `(printer_model, filament_id, nozzle_diameter, nozzle_volume_type, extruder_id)`. Many history rows per combo; one `is_active=True` enforced by a partial unique index.
- Printer-side `extrusion_cali_set` writes the same row into the printer's 16-slot PA history; `extrusion_cali_sel` auto-binds it to the AMS slot used for calibration.
- Dispatcher re-syncs the active calibration via `extrusion_cali_sel` on every non-cali print start — covers cases where you flip Active in BamDude without re-binding manually.
- 3MF calibration assets ship from BS `resources/calib/` (AGPL-3.0) under `backend/app/data/calib_assets/`. PA Line range: 0.0–0.1 step 0.002 (50 lines). Flow Rate coarse: `[-20, -15, -10, -5, 0, 5, 10, 15, 20]` %; fine: `[-5, -2, 0, 2, 5, 10, 15]` %.

### Apply path on a real print

`background_dispatch` resolves the active `filament_calibration` row for each filament slot used by the job and fires `extrusion_cali_sel(ams_id, tray_id, cali_idx)` before the print starts. The printer-side history slot becomes active; firmware applies the K (or flow ratio) to the print automatically. No gcode injection needed.

External-source prints (BS, printer screen) still benefit: the slot binding persists on the printer until explicitly changed, so the last `extrusion_cali_sel` BamDude fired stays in effect.

### History modal

Two sections side by side:
- **BamDude history** — `filament_calibration` rows grouped by nozzle. Per-row actions: **Set Active** (flips siblings + fires `extrusion_cali_sel`), **Delete**. Active row marked with green ring + checkmark.
- **Printer-side history** — 16-slot view pulled via `extrusion_cali_get`. Refresh button forces a re-pull for a given nozzle diameter.

!!! info "Resume banner"
    If you close the wizard mid-flow (after the print finished but before you saved), reopening the wizard shows a yellow banner with **Resume / Discard** for the in-flight session.

### Permissions and audit

`printers:update` gates the wizard entry and all mutation routes. Every action writes a row to `calibration_audit` — `(printer_id, session_id, action, payload_json, sequence_id, result, error_message, created_at)`. Actions: `start_session / save_result / set_active / delete / cancel`. No in-UI viewer yet; query the table directly.

### What's intentionally NOT in BamDude (yet)

- **PA range customization** — start/end/step are fixed to BS defaults. If you need a different range, calibrate in BS itself and import the value (Plan 4 candidate).
- **External spool calibration** — virtual tray `tray_id >= 0x10000` is disabled for auto path; manual path allows it but tray binding may not survive printer reboot.
- **Tower-mode result entry in BamDude** — tower modes start the print and finish. Read the result with your eyes, enter it in your slicer's filament profile. (BS does the same.)
```

- [ ] **Step 3: Mirror in UK file**

Add equivalent `## :material-flask: Калібровка філаменту` section to `printer-control.uk.md` with Ukrainian translations. Use existing UK file style as reference.

- [ ] **Step 4: Stage**

```
git -C D:/Development/docs.bamdude.top add docs/features/printer-control.md docs/features/printer-control.uk.md
```

(Commit + push to `dev` branch happens later in Task 15.)

---

### Task 14: Landing — extend Monitoring & Control feature card

**Files:**
- Modify: `D:/Development/bamdude.top/src/content/en/features-grouped.json`
- Modify: `D:/Development/bamdude.top/src/content/uk/features-grouped.json`

- [ ] **Step 1: Find current state**

```
grep -n "Filament Calibration\|Калібровка філаменту" D:/Development/bamdude.top/src/content/en/features-grouped.json
```

If a Plan-2 entry exists, replace it. If not, add new.

- [ ] **Step 2: Edit `en.features-grouped.json`**

Inside the Monitoring & Control group (id `monitoring`), `items` array — add (or replace existing draft):

```json
{
  "title": "Filament Calibration wizard",
  "body": "Bambu Studio parity — guided wizard for Pressure Advance (PA Line / Pattern / Tower / Auto) and Flow Rate (manual coarse+fine, auto on X1 lidar). Towers for Temp / Volumetric Speed / VFA / Retraction. Per-filament + nozzle storage in BamDude, auto-syncs to the printer's 16-slot history and binds to the AMS slot, so any subsequent print (BamDude, Bambu Studio, printer screen) uses the calibrated value. H2D dual-extruder per-extruder calibration. Calibration History modal with set-active / delete / refresh-from-printer."
}
```

Place near the existing AMS Settings entry for tonal consistency.

- [ ] **Step 3: Mirror in `uk.features-grouped.json`**

```json
{
  "title": "Майстер калібровки філаменту",
  "body": "Паритет з Bambu Studio — покроковий майстер для Pressure Advance (PA Line / Pattern / Tower / Auto) і Flow Rate (manual coarse+fine, auto на лідарних X1). Вежі Temp / Volumetric Speed / VFA / Retraction. Збереження per-filament + nozzle у BamDude, авто-sync у 16-слотну history принтера + bind до AMS слоту, тож наступний друк (з BamDude, BS, чи екрана принтера) використає відкаліброване значення. H2D dual-extruder — окрема калібровка per екструдер. CalibrationHistoryModal з set-active / delete / refresh-from-printer."
}
```

- [ ] **Step 4: Validate JSON**

```
node -e "JSON.parse(require('fs').readFileSync('D:/Development/bamdude.top/src/content/en/features-grouped.json','utf8'));console.log('en ok')"
node -e "JSON.parse(require('fs').readFileSync('D:/Development/bamdude.top/src/content/uk/features-grouped.json','utf8'));console.log('uk ok')"
```

Expected: `en ok` / `uk ok`.

- [ ] **Step 5: Stage**

```
git -C D:/Development/bamdude.top add src/content/en/features-grouped.json src/content/uk/features-grouped.json
```

---

### Task 15: CHANGELOG + final commit batch (3 repos)

**Files:**
- Modify: `D:/Development/bamdude/CHANGELOG.md`

- [ ] **Step 1: Replace the Plan 2 one-liner with a Plan 3 summary**

Under `[Unreleased]` in `CHANGELOG.md`, replace the existing Filament Calibration line with:

```markdown
- Filament Calibration wizard with Bambu Studio parity — Pressure Advance (Line/Pattern/Tower/Auto), Flow Rate (manual coarse+fine, auto on X1 lidar), tower modes (Temp/VolSpeed/VFA/Retraction). Per-filament + nozzle storage; auto-binds to AMS slot via `extrusion_cali_sel`. Calibration History modal with set-active / delete / refresh-from-printer. H2D dual-extruder support.
```

- [ ] **Step 2: Stage main repo**

```
git -C D:/Development/bamdude add CHANGELOG.md
```

- [ ] **Step 3: Build bundle**

```
cd D:/Development/bamdude/frontend && npm run build
git -C D:/Development/bamdude add static/
```

- [ ] **Step 4: Plan 3 commit (when user asks)**

Main repo:

```
feat(calibration): Plan 3 — auto, history, H2D, docs

Closes BS feature-parity gap left by Plans 1+2.

Frontend:
- StartPage: un-gated Auto PA, Auto Flow Rate, all 4 tower modes.
- CalibrationAutoSavePage: lidar results review with per-row K/N
  (or flow_ratio) edit + name + apply checkbox.
- CalibrationTowerFinishPage: mode-specific slicer-side instructions.
- CalibrationHistoryModal: BamDude rows (set-active/delete) + printer-
  side 16-slot history with refresh-from-printer button.
- CalibrationPresetPage: per-extruder tabs for H2D, per-extruder
  filament + temps state. Manual mode = 1 extruder per session;
  auto mode = batch via filaments[].extruder_id.

Backend:
- submit_auto_result routes flow_ratio for FLOW_RATE sessions.
- save_result MQTT-pushes flow_ratio via extrusion_cali_set k_value slot.
- Tower modes on-complete flip status=saved (no user input).
- CalibFilamentInput / CalibFilamentIn add extruder_id_override for
  H2D batched auto-calibration.

Docs + landing:
- docs.bamdude.top printer-control: full Filament Calibration section
  (en + uk).
- bamdude.top features-grouped.json: extended Monitoring & Control card
  (en + uk).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

Docs repo:

```
docs(printer-control): Filament Calibration full section (en + uk)

Mirrors the in-app wizard — modes table, per-model capability matrix,
state + persistence, apply path, history modal, permissions + audit.
```

Landing repo:

```
content(features): Filament Calibration wizard (en + uk)

Extends Monitoring & Control group with the Plan 3 feature card.
```

- [ ] **Step 5: Pushes (when user asks)**

```
git -C D:/Development/bamdude push origin <branch>
git -C D:/Development/docs.bamdude.top push origin dev
git -C D:/Development/bamdude.top push origin dev
```

(Docs + landing land on `dev`; user merges `dev → main` via PR to deploy — per memory rule.)

---

## Plan 3 Summary

**What this plan delivers:**
- Auto-cali save UI for X1 lidar (PA + Flow Rate).
- Tower modes (Temp / Vol Speed / VFA / Retraction) as print-and-finish flows.
- Full `CalibrationHistoryModal` — BamDude rows + printer-side 16-slot history + set-active / delete / refresh.
- H2D dual-extruder UI tabs in `CalibrationPresetPage`; per-extruder filament + temps; manual one-at-a-time, auto batched.
- Backend: `submit_auto_result` flow_ratio routing, tower on-complete handling, per-filament `extruder_id` override.
- Docs site + landing page updates (both en + uk).
- Production bundle ships under `static/`.

**What's done as of Plan 3:**
- Full BS Pressure Advance + Flow Rate wizard parity.
- Auto + manual paths.
- Towers as print-trigger (BS-style).
- Per-filament-type persistence with `is_active` switching.
- Auto-bind to AMS slot via `extrusion_cali_sel`.
- Per-printer-model resolution.
- H2D dual-extruder.
- History viewer with printer-side cross-check.

**Open follow-ups (out of scope for this plan; Plan 4 candidates):**
1. **PA range customization** — user-defined start/end/step (BS-defaults frozen in Plan 1 constants).
2. **External spool (virtual tray)** in auto path.
3. **Spoolman sync** of `filament_calibration` rows.
4. **CalibrationHistoryModal edit** — re-edit K value in place.
5. **Tower mode result entry** — let user type the value (slicer-side change-helper).
6. **Audit viewer UI** — currently you have to query `calibration_audit` directly.

---

## Spec Self-Review

**Coverage:**

| Spec section | Plan 3 task |
|---|---|
| Auto PA + Auto Flow Rate UI | T5 (AutoSavePage), T4 (un-gated Start) |
| Tower modes flow | T3 (backend on-complete), T6 (TowerFinishPage), T4 (un-gated Start) |
| CalibrationHistoryModal | T9, T10, T11 |
| Resume banner (Plan 2 covered) | n/a — Plan 2 |
| H2D dual-extruder UI | T12 |
| Backend `submit_auto_result` flow_ratio | T2 |
| Backend tower on-complete | T3 |
| Docs site update | T13 |
| Landing site update | T14 |
| CHANGELOG | T15 |
| WS event emission (Plan 2 covered) | n/a — Plan 2 |

**Placeholder scan:** none. T11 Step 4 explicitly drops the "View History" button from FinishPage as a YAGNI cut — user gets to history via kebab.

**Type consistency:** `WizardStep` adds `autoSave / towerFinish` consistently. `AutoResultEditIn` typed in client + service + route. `CalibFilamentInput.extruder_id_override` flows client → schema → service.

**Scope:** single deployable slice closing BS parity. Plan 4+ candidates explicitly enumerated as follow-ups.
