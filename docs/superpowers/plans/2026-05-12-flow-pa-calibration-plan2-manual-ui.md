# Flow Rate / PA Calibration — Plan 2 (Manual UI)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Frontend wizard for manual PA + Flow Rate calibration, callable by P1S / A1 / A1 mini / X1C-without-lidar users. Covers the bulk of the fleet. Auto-path UI (X1 lidar), Tower modes, History modal, dual-extruder UI all deferred to Plan 3.

**Architecture:** Multi-step modal mirroring BS `PressureAdvanceWizard` / `FlowRateWizard` manual path. State machine driven by `useFilamentCalibration` hook; auto-advances on WS `calibration.*` events emitted from the backend dispatch hook. Frontend talks to Plan 1's REST API; no new backend endpoints, only WS event emission and one resume-banner-friendly query.

**Tech Stack:** React 19 + Router 7 + TanStack Query 5 + Tailwind 4 (`bg-bambu-dark*` CSS vars) + react-i18next + lucide-react icons. Existing patterns: `PrinterSettingsModal` (modal shell), `useWebSocket` (WS plumbing), `usePrinterSettings` (TanStack mutation pattern).

**Spec:** `docs/superpowers/specs/2026-05-12-flow-pa-calibration-design.md`. Plan 1: `docs/superpowers/plans/2026-05-12-flow-pa-calibration-plan1-foundation.md` — backend dependencies, must ship before Plan 2 execution.

**Out of scope (Plan 3):**
- Auto save page (X1 lidar pre-filled values).
- Tower modes (Temp / Vol Speed / VFA / Retraction) — disabled in Start page with "Plan 3" tooltip.
- Full `CalibrationHistoryModal`.
- H2D dual-extruder UI (Plan 2 hard-codes extruder_id=0).
- Docs site + landing page updates.
- "External spool" (virtual tray) selection — disabled in Preset.

**User workflow notes:**
- Per-wave verify (run `npm run lint && npx tsc --noEmit && npm run test:run` once at end of wave).
- Commits only when user explicitly asks.
- All conversation in Ukrainian; code/docs/commits in English.
- i18n: keys land in both `en.ts` AND `uk.ts` in the same task.

---

## File Map

**New frontend files (10):**
- `frontend/src/components/FilamentCalibrationModal.tsx` — wizard shell
- `frontend/src/components/calibration/CalibrationStartPage.tsx`
- `frontend/src/components/calibration/CalibrationPresetPage.tsx`
- `frontend/src/components/calibration/CalibrationRunningPage.tsx`
- `frontend/src/components/calibration/CalibrationManualSavePage.tsx`
- `frontend/src/components/calibration/CalibrationCoarseSavePage.tsx`
- `frontend/src/components/calibration/CalibrationFineSavePage.tsx`
- `frontend/src/components/calibration/CalibrationFinishPage.tsx`
- `frontend/src/components/calibration/ResumeBanner.tsx`
- `frontend/src/hooks/useFilamentCalibration.ts`

**Modified frontend files (5):**
- `frontend/src/api/client.ts` — new types + client methods
- `frontend/src/i18n/locales/en.ts` — `filamentCali` namespace
- `frontend/src/i18n/locales/uk.ts` — same
- `frontend/src/pages/PrintersPage.tsx` — kebab menu entry + modal mount
- `frontend/src/hooks/useWebSocket.ts` — route `calibration.*` messages

**New tests (3):**
- `frontend/src/__tests__/components/FilamentCalibrationModal.test.tsx`
- `frontend/src/__tests__/components/CalibrationStartPage.test.tsx`
- `frontend/src/__tests__/hooks/useFilamentCalibration.test.tsx`

**Modified backend files (2):**
- `backend/app/services/background_dispatch.py` — emit `calibration.*` WS events
- `backend/app/services/calibration_service.py` — emit `calibration.completed` on save
- `backend/tests/unit/services/test_calibration_service.py` — assert WS emit

---

## Wave 1 — API client + i18n base + kebab wire-up

### Task 1: Extend `api/client.ts` with calibration types + methods

**Files:**
- Modify: `frontend/src/api/client.ts`

- [ ] **Step 1: Locate existing pattern**

Grep: `grep -n "getPrinterSettings\|postPrinterSettings" frontend/src/api/client.ts`. Note location.

- [ ] **Step 2: Append types + methods**

Add to `client.ts` (near other types):

```typescript
// ---------- Filament Calibration ----------

export type CaliMode =
  | 'pa_line' | 'pa_pattern' | 'pa_tower' | 'auto_pa_line'
  | 'flow_rate'
  | 'temp_tower' | 'vol_speed_tower' | 'vfa_tower' | 'retraction_tower';

export type CaliMethod = 'auto' | 'manual';

export type NozzleVolumeType = 'standard' | 'high_flow' | 'tpu_high_flow' | 'hybrid';

export interface CalibCapabilities {
  pa_manual: boolean;
  flow_manual: boolean;
  temp_tower: boolean;
  vol_speed_tower: boolean;
  vfa_tower: boolean;
  retraction_tower: boolean;
  pa_auto: boolean;
  flow_auto: boolean;
  dual_extruder: boolean;
  extruders: Array<{ id: number; name: string }>;
  nozzles: Array<{ id: number; diameter: number | null; type: string | null; flow_type: string | null }>;
}

export interface CalibFilamentIn {
  ams_id: number;
  slot_id: number;
  tray_id: number;
  filament_id: string;
  filament_setting_id?: string | null;
  bed_temp: number;
  nozzle_temp: number;
  max_volumetric_speed: number;
  flow_rate?: number;
}

export interface StartSessionIn {
  cali_mode: CaliMode;
  method: CaliMethod;
  nozzle_diameter: number;
  nozzle_volume_type: NozzleVolumeType;
  extruder_id?: number;
  filaments: CalibFilamentIn[];
}

export interface CalibrationSessionOut {
  id: number;
  printer_id: number;
  user_id: number | null;
  cali_mode: string;
  method: string;
  nozzle_diameter: number;
  nozzle_volume_type: string;
  extruder_id: number;
  status: 'running' | 'awaiting_user_input' | 'saved' | 'cancelled' | 'failed';
  stage: number;
  coarse_ratio: number | null;
  parent_session_id: number | null;
  mqtt_sequence_id: string | null;
  print_queue_item_id: number | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface FilamentCalibrationOut {
  id: number;
  printer_model: string;
  filament_id: string;
  filament_setting_id: string | null;
  nozzle_diameter: number;
  nozzle_volume_type: string;
  extruder_id: number;
  pa_k_value: number | null;
  pa_n_coef: number | null;
  flow_ratio: number | null;
  confidence: number | null;
  cali_mode: string;
  source: string;
  is_active: boolean;
  cali_idx: number | null;
  name: string;
  notes: string | null;
  calibrated_on_printer_id: number | null;
  calibrated_by_user_id: number | null;
  created_at: string;
}

export interface ManualResultIn {
  best_line_index?: number;
  coarse_modifier?: number;
  skip_fine?: boolean;
  fine_modifier?: number;
}

export interface ManualResultOut {
  saved_rows: FilamentCalibrationOut[];
  next_session_id: number | null;
}
```

Then add to the `api` object methods near the bottom:

```typescript
getCalibrationCapabilities: (printerId: number) =>
  request<CalibCapabilities>(`/printers/${printerId}/calibration/capabilities`),

startCalibrationSession: (printerId: number, body: StartSessionIn) =>
  request<CalibrationSessionOut>(`/printers/${printerId}/calibration/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }),

getCalibrationSession: (sessionId: number) =>
  request<CalibrationSessionOut>(`/calibration/sessions/${sessionId}`),

cancelCalibrationSession: (sessionId: number) =>
  request<void>(`/calibration/sessions/${sessionId}/cancel`, { method: 'POST' }),

submitManualResult: (sessionId: number, body: ManualResultIn) =>
  request<ManualResultOut>(`/calibration/sessions/${sessionId}/manual-result`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }),

listAwaitingSessions: (printerId: number) =>
  request<CalibrationSessionOut[]>(
    `/calibration/sessions?printer_id=${printerId}&status=awaiting_user_input`,
  ),
```

- [ ] **Step 3: Type check**

Run: `cd frontend && npx tsc --noEmit`
Expected: no new errors.

- [ ] **Step 4: Stage**

`git add frontend/src/api/client.ts`

---

### Task 2: i18n base — `filamentCali` namespace

**Files:**
- Modify: `frontend/src/i18n/locales/en.ts`
- Modify: `frontend/src/i18n/locales/uk.ts`

- [ ] **Step 1: Add EN keys**

Append to `frontend/src/i18n/locales/en.ts` after the `printerSettings` block (find line via `grep -n "printerSettings:" frontend/src/i18n/locales/en.ts`):

```typescript
filamentCali: {
  menuItem: 'Filament Calibration',
  title: 'Filament Calibration',
  // Wizard step labels (top progress)
  step: {
    start: 'Mode',
    preset: 'Filament',
    running: 'Calibrating',
    save: 'Save',
    coarse: 'Coarse save',
    fine: 'Fine calibration',
    finish: 'Done',
  },
  // Start page
  start: {
    heading: 'What do you want to calibrate?',
    paGroup: 'Pressure Advance',
    flowGroup: 'Flow Rate',
    towerGroup: 'Towers (Plan 3)',
    paLine: 'PA Line (manual, 50 lines)',
    paLineDesc: 'Print a tower of 50 lines, each with a different Pressure Advance value. Pick the cleanest line at the end.',
    paPattern: 'PA Pattern (manual, grid)',
    paPatternDesc: 'A grid of PA values printed flat — easier on bowden setups. (Comes in Plan 3.)',
    paTower: 'PA Tower (manual, tower)',
    paTowerDesc: 'A vertical tower with stepped PA. (Comes in Plan 3.)',
    paAuto: 'Auto PA (X1 lidar)',
    paAutoDesc: 'Printer prints + scans + computes K automatically. Requires lidar. (Comes in Plan 3.)',
    flowRate: 'Flow Rate (manual, coarse + fine)',
    flowRateDesc: 'Two-stage 9-block test. Coarse picks best block from −20 % to +20 %; fine refines around it.',
    flowAuto: 'Auto Flow Rate (X1 lidar)',
    flowAutoDesc: 'Printer scans printed lines and reports flow ratio. (Comes in Plan 3.)',
    notSupported: 'Not supported on this printer',
    comingInPlan3: 'Coming in Plan 3',
  },
  // Preset page
  preset: {
    heading: 'Choose filament + settings',
    nozzleSection: 'Nozzle',
    nozzleDia: 'Diameter',
    nozzleType: 'Type',
    bedTemp: 'Bed temp, °C',
    nozzleTemp: 'Nozzle temp, °C',
    maxVolSpeed: 'Max volumetric speed, mm³/s',
    selectFilament: 'Select a loaded spool',
    externalSpoolDisabled: 'External spool calibration is not supported in this plan',
    noLoadedSlot: 'No loaded AMS slot — load a spool first',
    missingTemps: 'Set nozzle and bed temperature',
    customFilament: 'Custom filament — name:',
    customFilamentPlaceholder: 'My PETG',
  },
  // Running page
  running: {
    heading: 'Calibrating…',
    waitingForStart: 'Waiting for the printer to start',
    inProgress: 'Calibration in progress',
    cancelConfirm: 'Cancel calibration and stop the print?',
    cancel: 'Cancel calibration',
  },
  // Manual save (PA)
  manualSave: {
    heading: 'Pick the best-looking line',
    instruction: 'Look at the printed tower. Count lines bottom-to-top (line 0 is the first); pick the one with cleanest extrusion.',
    lineIndex: 'Line index',
    computedK: 'Computed K value',
    name: 'Save as',
    namePlaceholder: 'PLA Basic — PA 0.048',
    notes: 'Notes',
    notesPlaceholder: 'Anything to remember about this calibration',
    syncToPrinter: 'Save to printer history (lets external prints use it)',
    save: 'Save & finish',
  },
  // Coarse save (Flow Rate)
  coarseSave: {
    heading: 'Pick the smoothest top surface',
    instruction: 'Inspect the top surface of each of the 9 blocks. Pick the one with the smoothest infill — no gaps, no ridges.',
    blockModifier: 'Best block',
    coarseRatio: 'Coarse flow ratio',
    skipFine: 'Skip fine calibration (use this value directly)',
    continue: 'Continue to fine',
    saveAndFinish: 'Save & finish',
  },
  // Fine save
  fineSave: {
    heading: 'Pick the best fine block',
    instruction: 'Pick the block with the cleanest top surface among the 7 fine-step blocks.',
    fineModifier: 'Best block',
    fineRatio: 'Final flow ratio',
    save: 'Save & finish',
  },
  // Finish
  finish: {
    heading: 'Saved',
    body: 'Active on the next print using this filament + nozzle combination.',
    viewHistory: 'View history',
    calibrateAnother: 'Calibrate another',
    close: 'Close',
  },
  // Resume banner
  resume: {
    title: 'Unfinished calibration',
    bodyPaLine: '{{filament}} · PA Line · {{date}}',
    bodyFlow: '{{filament}} · Flow Rate ({{stage}}) · {{date}}',
    resume: 'Resume',
    discard: 'Discard',
    discardConfirm: 'Discard this unfinished calibration session?',
  },
  // Errors
  err: {
    activeSession: 'Another calibration is already running on this printer',
    notOnline: 'Printer is offline',
    publishFailed: 'Failed to send MQTT command',
    sessionNotAwaiting: 'Session is not in the expected state',
    saveFailed: 'Failed to save calibration',
    cancelFailed: 'Failed to cancel calibration',
  },
  // Generic
  back: 'Back',
  next: 'Next',
  startCalibration: 'Start calibration',
},
```

- [ ] **Step 2: Add UK keys (mirror)**

Append to `frontend/src/i18n/locales/uk.ts` at the matching location:

```typescript
filamentCali: {
  menuItem: 'Калібровка філаменту',
  title: 'Калібровка філаменту',
  step: {
    start: 'Режим',
    preset: 'Філамент',
    running: 'Калібровка',
    save: 'Зберегти',
    coarse: 'Грубий етап',
    fine: 'Точний етап',
    finish: 'Готово',
  },
  start: {
    heading: 'Що калібруємо?',
    paGroup: 'Pressure Advance',
    flowGroup: 'Flow Rate',
    towerGroup: 'Тестові вежі (Plan 3)',
    paLine: 'PA Line (manual, 50 ліній)',
    paLineDesc: 'Друкуємо вежу з 50 ліній, кожна з різним PA. По завершенню обираєш найчистішу.',
    paPattern: 'PA Pattern (manual, сітка)',
    paPatternDesc: 'Сітка PA-значень плоско — зручно для bowden. (У Plan 3.)',
    paTower: 'PA Tower (manual, вежа)',
    paTowerDesc: 'Вертикальна вежа з поступовою зміною PA. (У Plan 3.)',
    paAuto: 'Auto PA (лідар X1)',
    paAutoDesc: 'Принтер сам друкує + сканує + рахує K. Потрібен лідар. (У Plan 3.)',
    flowRate: 'Flow Rate (manual, грубий + точний)',
    flowRateDesc: 'Двоетапний 9-блочний тест. Грубий етап обирає блок від −20 % до +20 %; точний уточнює.',
    flowAuto: 'Auto Flow Rate (лідар X1)',
    flowAutoDesc: 'Принтер сам сканує і повертає flow ratio. (У Plan 3.)',
    notSupported: 'Не підтримується на цьому принтері',
    comingInPlan3: 'У Plan 3',
  },
  preset: {
    heading: 'Філамент + параметри',
    nozzleSection: 'Сопло',
    nozzleDia: 'Діаметр',
    nozzleType: 'Тип',
    bedTemp: 'Температура столу, °C',
    nozzleTemp: 'Температура сопла, °C',
    maxVolSpeed: 'Макс. об\'ємна швидкість, мм³/с',
    selectFilament: 'Обери завантажену котушку',
    externalSpoolDisabled: 'Калібровка зовнішньої котушки у цьому плані не підтримується',
    noLoadedSlot: 'Жодного завантаженого AMS слоту — спочатку завантаж котушку',
    missingTemps: 'Вкажи температури сопла і столу',
    customFilament: 'Власний філамент — назва:',
    customFilamentPlaceholder: 'Мій PETG',
  },
  running: {
    heading: 'Калібровка триває…',
    waitingForStart: 'Чекаємо старту принтера',
    inProgress: 'Друк калібровки',
    cancelConfirm: 'Скасувати калібровку і зупинити друк?',
    cancel: 'Скасувати калібровку',
  },
  manualSave: {
    heading: 'Обери найчистішу лінію',
    instruction: 'Подивись на надруковану вежу. Рахуй лінії знизу вгору (лінія 0 — перша); обери з найчистішою екструзією.',
    lineIndex: 'Номер лінії',
    computedK: 'Обчислене K',
    name: 'Зберегти як',
    namePlaceholder: 'PLA Basic — PA 0.048',
    notes: 'Нотатки',
    notesPlaceholder: 'Що варто запам\'ятати про цю калібровку',
    syncToPrinter: 'Зберегти в history принтера (для друків з інших джерел теж застосовуватиметься)',
    save: 'Зберегти і завершити',
  },
  coarseSave: {
    heading: 'Обери найгладкіший верхній шар',
    instruction: 'Подивись на верхню поверхню кожного з 9 блоків. Обери з найгладшим інфілом — без щілин і гребенів.',
    blockModifier: 'Найкращий блок',
    coarseRatio: 'Грубий flow ratio',
    skipFine: 'Пропустити точний етап (зберегти грубе значення)',
    continue: 'Перейти до точного',
    saveAndFinish: 'Зберегти і завершити',
  },
  fineSave: {
    heading: 'Обери точний блок',
    instruction: 'Обери блок з найчистішою верхньою поверхнею серед 7 точних блоків.',
    fineModifier: 'Найкращий блок',
    fineRatio: 'Остаточний flow ratio',
    save: 'Зберегти і завершити',
  },
  finish: {
    heading: 'Збережено',
    body: 'Активно для наступного друку з цим філаментом + соплом.',
    viewHistory: 'Переглянути історію',
    calibrateAnother: 'Відкалібрувати щось ще',
    close: 'Закрити',
  },
  resume: {
    title: 'Незавершена калібровка',
    bodyPaLine: '{{filament}} · PA Line · {{date}}',
    bodyFlow: '{{filament}} · Flow Rate ({{stage}}) · {{date}}',
    resume: 'Продовжити',
    discard: 'Скасувати',
    discardConfirm: 'Скасувати незавершену калібровку?',
  },
  err: {
    activeSession: 'На цьому принтері вже триває інша калібровка',
    notOnline: 'Принтер офлайн',
    publishFailed: 'Не вдалось надіслати MQTT-команду',
    sessionNotAwaiting: 'Сесія не очікує введення',
    saveFailed: 'Не вдалось зберегти калібровку',
    cancelFailed: 'Не вдалось скасувати калібровку',
  },
  back: 'Назад',
  next: 'Далі',
  startCalibration: 'Почати калібровку',
},
```

- [ ] **Step 3: Type check**

Run: `cd frontend && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Stage**

`git add frontend/src/i18n/locales/en.ts frontend/src/i18n/locales/uk.ts`

---

### Task 3: PrintersPage kebab item + modal mount placeholder

**Files:**
- Modify: `frontend/src/pages/PrintersPage.tsx`

- [ ] **Step 1: Find existing kebab section**

Grep: `grep -n "PrinterSettingsModal\|setShowPrinterSettings" frontend/src/pages/PrintersPage.tsx`. Note section.

- [ ] **Step 2: Add state + import**

Near top of the component, add state alongside existing modal-state variables:

```typescript
const [showFilamentCali, setShowFilamentCali] = useState<number | null>(null);
```

Add import at top of file:

```typescript
import { FilamentCalibrationModal } from '../components/FilamentCalibrationModal';
```

- [ ] **Step 3: Add kebab menu entry**

In the kebab menu items list (find the place where `PrinterSettings` menu item lives), add after it:

```tsx
{user?.hasPermission?.('printers:update') && (
  <button
    onClick={() => setShowFilamentCali(printer.id)}
    disabled={!printer.online}
    className="w-full text-left px-3 py-2 text-sm hover:bg-bambu-dark-tertiary disabled:opacity-50 disabled:cursor-not-allowed"
  >
    {t('filamentCali.menuItem')}
  </button>
)}
```

(Match the existing item's style/component — use the same wrapper component if a `KebabMenuItem` exists.)

- [ ] **Step 4: Mount modal**

At the bottom of the component's JSX (alongside `<PrinterSettingsModal …/>`):

```tsx
{showFilamentCali !== null && (
  <FilamentCalibrationModal
    isOpen
    printerId={showFilamentCali}
    onClose={() => setShowFilamentCali(null)}
  />
)}
```

- [ ] **Step 5: Create stub component to satisfy build**

To keep the build green before Task 4, create a placeholder:

```tsx
// frontend/src/components/FilamentCalibrationModal.tsx
import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';

interface Props { isOpen: boolean; onClose: () => void; printerId: number; }

export function FilamentCalibrationModal({ isOpen, onClose }: Props) {
  const { t } = useTranslation();
  if (!isOpen) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl p-6 w-full max-w-2xl mx-4">
        <div className="flex justify-between mb-4">
          <h2 className="text-lg font-semibold text-white">{t('filamentCali.title')}</h2>
          <button onClick={onClose} aria-label="Close" className="text-bambu-gray hover:text-white">
            <X className="h-5 w-5" />
          </button>
        </div>
        <p className="text-bambu-gray">Wizard coming next task.</p>
      </div>
    </div>
  );
}
```

- [ ] **Step 6: Type check + lint**

Run: `cd frontend && npx tsc --noEmit && npm run lint`
Expected: no errors.

- [ ] **Step 7: Stage**

`git add frontend/src/pages/PrintersPage.tsx frontend/src/components/FilamentCalibrationModal.tsx`

---

### Wave 1 verify

- [ ] Type check + lint + build

```
cd frontend && npx tsc --noEmit && npm run lint && npm run build
```

Expected: green, bundle updates under `static/`.

- [ ] Manual sanity: open PrintersPage in browser, click kebab on an online printer, see "Filament Calibration" menu item, click it — placeholder modal opens with title.

- [ ] Wave 1 commit (when user asks):

```
feat(calibration): API client + i18n + kebab entry (Plan 2 Wave 1)

- frontend api/client: types + 6 calibration methods.
- en+uk i18n: filamentCali namespace (~80 keys).
- PrintersPage kebab menu: "Filament Calibration" entry, gated by
  printers:update + online.
- Placeholder modal component — full wizard in Wave 2-4.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Wave 2 — Hook + WS plumbing + Wizard shell

### Task 4: Backend — emit `calibration.*` WS events

**Files:**
- Modify: `backend/app/services/calibration_service.py`
- Modify: `backend/app/services/background_dispatch.py`
- Modify: `backend/tests/unit/services/test_calibration_service.py`

- [ ] **Step 1: Locate existing WS broadcast helper**

Grep: `grep -rn "broadcast_to_printer\|broadcast.*printer.*type" backend/app/ | head -10`. Note the helper used by other features.

If no obvious helper, look for `websocket` in `backend/app/core/websocket.py`.

- [ ] **Step 2: Test for emit on save**

Append to `backend/tests/unit/services/test_calibration_service.py`:

```python
@pytest.mark.asyncio
async def test_save_result_emits_completed_ws_event(service, db_session, printer_factory, mock_client):
    printer = await printer_factory(model="P1S")
    from backend.app.models.calibration_session import CalibrationSession
    s = CalibrationSession(
        printer_id=printer.id, user_id=None,
        cali_mode="pa_line", method="manual",
        nozzle_diameter=0.4, nozzle_volume_type="standard", extruder_id=0,
        filaments_json='[{"ams_id":0,"slot_id":0,"tray_id":0,"filament_id":"GFG00","setting_id":""}]',
        status="awaiting_user_input", stage=1,
    )
    db_session.add(s); await db_session.commit(); await db_session.refresh(s)

    mock_client.extrusion_cali_set = MagicMock(return_value=(True, "S"))
    mock_client.extrusion_cali_sel = MagicMock(return_value=(True, "S"))

    with patch("backend.app.services.calibration_service.printer_manager") as pm, \
         patch("backend.app.services.calibration_service.broadcast_calibration_event") as bce:
        pm.get_client.return_value = mock_client
        bce.return_value = None
        await service.submit_manual_result(db=db_session, session_id=s.id, best_line_index=24)

    bce.assert_called()
    call = bce.call_args.kwargs
    assert call["event"] in ("completed", "saved")
    assert call["printer_id"] == printer.id
```

- [ ] **Step 3: Add `broadcast_calibration_event` helper**

In `backend/app/services/calibration_service.py`, top of file:

```python
import asyncio

from backend.app.core.websocket import broadcast_to_printer  # adjust if name differs


async def broadcast_calibration_event(
    *, printer_id: int, event: str, payload: dict | None = None,
) -> None:
    """Wraps the existing WS broadcaster with a `calibration.*` message type."""
    try:
        await broadcast_to_printer(
            printer_id=printer_id,
            message={
                "type": f"calibration.{event}",
                "printer_id": printer_id,
                "data": payload or {},
            },
        )
    except Exception:
        # WS emission is best-effort; never break the save path
        pass
```

(If the existing helper has different signature, adapt the inner body but keep the outer signature stable so tests pass.)

- [ ] **Step 4: Wire emission**

In `save_result`, right before `return new_row`:

```python
await broadcast_calibration_event(
    printer_id=session.printer_id,
    event="saved",
    payload={"session_id": session.id, "filament_calibration_id": new_row.id},
)
```

In `start_calibration` (both auto + manual branches), right before `return session`:

```python
await broadcast_calibration_event(
    printer_id=printer_id,
    event="started",
    payload={"session_id": session.id, "cali_mode": cali_mode.value, "method": method.value},
)
```

In `cancel_session` after `s.status = "cancelled"`:

```python
await broadcast_calibration_event(
    printer_id=s.printer_id, event="cancelled",
    payload={"session_id": s.id},
)
```

In `background_dispatch.py` on print complete (where you flip session to awaiting_user_input):

```python
from backend.app.services.calibration_service import broadcast_calibration_event
await broadcast_calibration_event(
    printer_id=cs.printer_id, event="completed",
    payload={"session_id": cs.id},
)
```

In `background_dispatch.py` on print fail (HMS / error path), if `is_calibration`:

```python
await broadcast_calibration_event(
    printer_id=cs.printer_id, event="failed",
    payload={"session_id": cs.id, "error": "print_failed"},
)
```

- [ ] **Step 5: Run test**

Run: `pytest backend/tests/unit/services/test_calibration_service.py::test_save_result_emits_completed_ws_event -v`
Expected: passed.

Plus re-run wave-1 backend tests:
Run: `pytest backend/tests/unit/services/test_calibration_service.py -v`
Expected: all passed.

- [ ] **Step 6: Stage**

`git add backend/app/services/calibration_service.py backend/app/services/background_dispatch.py backend/tests/unit/services/test_calibration_service.py`

---

### Task 5: Frontend WS router — route `calibration.*` events

**Files:**
- Modify: `frontend/src/hooks/useWebSocket.ts`

- [ ] **Step 1: Locate message dispatcher**

Grep: `grep -n "handleMessage\|case 'printer_status'" frontend/src/hooks/useWebSocket.ts`. Find the switch/if-block dispatching by `message.type`.

- [ ] **Step 2: Add calibration branch**

In the message handler (likely inside `handleMessageRef.current = (message) => { ... }`):

```typescript
if (message.type?.startsWith('calibration.')) {
  // Invalidate session list + capabilities; downstream hook consumes data.
  // Tag with a CustomEvent so the wizard hook can advance step.
  queryClient.invalidateQueries({ queryKey: ['calibration', 'sessions'] });
  if (message.printer_id != null) {
    queryClient.invalidateQueries({ queryKey: ['calibration', 'awaiting', message.printer_id] });
  }
  window.dispatchEvent(new CustomEvent('calibration-event', { detail: message }));
  return;
}
```

- [ ] **Step 3: Type check**

Run: `cd frontend && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 4: Stage**

`git add frontend/src/hooks/useWebSocket.ts`

---

### Task 6: `useFilamentCalibration` hook (state machine)

**Files:**
- Create: `frontend/src/hooks/useFilamentCalibration.ts`
- Test: `frontend/src/__tests__/hooks/useFilamentCalibration.test.tsx`

- [ ] **Step 1: Test for step transitions**

```typescript
// frontend/src/__tests__/hooks/useFilamentCalibration.test.tsx
import { describe, expect, it } from 'vitest';
import { computeNextStep } from '../../hooks/useFilamentCalibration';

describe('computeNextStep', () => {
  it('start → preset', () => {
    expect(computeNextStep('start', { cali_mode: 'pa_line', method: 'manual' })).toBe('preset');
  });

  it('preset → running', () => {
    expect(computeNextStep('preset', { cali_mode: 'pa_line', method: 'manual', sessionStarted: true })).toBe('running');
  });

  it('running PA → manualSave on completed', () => {
    expect(
      computeNextStep('running', {
        cali_mode: 'pa_line', method: 'manual', sessionStatus: 'awaiting_user_input',
      }),
    ).toBe('manualSave');
  });

  it('running Flow Rate stage 1 → coarseSave on completed', () => {
    expect(
      computeNextStep('running', {
        cali_mode: 'flow_rate', method: 'manual', stage: 1, sessionStatus: 'awaiting_user_input',
      }),
    ).toBe('coarseSave');
  });

  it('running Flow Rate stage 2 → fineSave on completed', () => {
    expect(
      computeNextStep('running', {
        cali_mode: 'flow_rate', method: 'manual', stage: 2, sessionStatus: 'awaiting_user_input',
      }),
    ).toBe('fineSave');
  });

  it('coarseSave → finish (skip fine path)', () => {
    expect(computeNextStep('coarseSave', { skipFine: true, savedRows: 1 })).toBe('finish');
  });

  it('coarseSave → running (continue fine path)', () => {
    expect(computeNextStep('coarseSave', { skipFine: false, nextSessionId: 99 })).toBe('running');
  });

  it('manualSave → finish on saved rows', () => {
    expect(computeNextStep('manualSave', { savedRows: 1 })).toBe('finish');
  });
});
```

- [ ] **Step 2: Run test, see failure**

Run: `cd frontend && npm run test:run -- src/__tests__/hooks/useFilamentCalibration.test.tsx`
Expected: FAIL (module not found).

- [ ] **Step 3: Write the hook**

```typescript
// frontend/src/hooks/useFilamentCalibration.ts
import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api } from '../api/client';
import type {
  CaliMethod, CaliMode, CalibCapabilities, CalibFilamentIn,
  CalibrationSessionOut, FilamentCalibrationOut, ManualResultIn,
  NozzleVolumeType,
} from '../api/client';

export type WizardStep =
  | 'start'
  | 'preset'
  | 'running'
  | 'manualSave'
  | 'coarseSave'
  | 'fineSave'
  | 'finish';

export interface ComputeNextStepInput {
  cali_mode?: CaliMode;
  method?: CaliMethod;
  sessionStarted?: boolean;
  sessionStatus?: CalibrationSessionOut['status'];
  stage?: number;
  skipFine?: boolean;
  savedRows?: number;
  nextSessionId?: number | null;
}

/** Pure helper — unit-tested separately. */
export function computeNextStep(current: WizardStep, ctx: ComputeNextStepInput): WizardStep {
  switch (current) {
    case 'start':
      return 'preset';
    case 'preset':
      return ctx.sessionStarted ? 'running' : 'preset';
    case 'running':
      if (ctx.sessionStatus !== 'awaiting_user_input') return 'running';
      if (ctx.cali_mode === 'flow_rate') {
        return ctx.stage === 2 ? 'fineSave' : 'coarseSave';
      }
      return 'manualSave';
    case 'coarseSave':
      if (ctx.skipFine) return ctx.savedRows ? 'finish' : 'coarseSave';
      if (ctx.nextSessionId != null) return 'running';
      return 'coarseSave';
    case 'manualSave':
    case 'fineSave':
      return ctx.savedRows ? 'finish' : current;
    case 'finish':
      return 'finish';
    default:
      return 'start';
  }
}

interface WizardInput {
  cali_mode: CaliMode;
  method: CaliMethod;
  nozzle_diameter: number;
  nozzle_volume_type: NozzleVolumeType;
  extruder_id: number;
  filaments: CalibFilamentIn[];
}

export function useFilamentCalibration(printerId: number, enabled: boolean) {
  const qc = useQueryClient();
  const [step, setStep] = useState<WizardStep>('start');
  const [input, setInput] = useState<Partial<WizardInput>>({ extruder_id: 0 });
  const [sessionId, setSessionId] = useState<number | null>(null);
  const [savedRows, setSavedRows] = useState<FilamentCalibrationOut[]>([]);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  // Capabilities
  const capQuery = useQuery<CalibCapabilities>({
    queryKey: ['calibration', 'capabilities', printerId],
    queryFn: () => api.getCalibrationCapabilities(printerId),
    enabled,
    staleTime: 30_000,
  });

  // Active session (for resume)
  const awaitingQuery = useQuery<CalibrationSessionOut[]>({
    queryKey: ['calibration', 'awaiting', printerId],
    queryFn: () => api.listAwaitingSessions(printerId),
    enabled,
    staleTime: 5_000,
  });

  // Session itself (when ID known)
  const sessionQuery = useQuery<CalibrationSessionOut>({
    queryKey: ['calibration', 'session', sessionId],
    queryFn: () => api.getCalibrationSession(sessionId!),
    enabled: sessionId != null,
    staleTime: 1_000,
  });

  // Start session
  const startMutation = useMutation({
    mutationFn: (body: WizardInput) =>
      api.startCalibrationSession(printerId, {
        cali_mode: body.cali_mode,
        method: body.method,
        nozzle_diameter: body.nozzle_diameter,
        nozzle_volume_type: body.nozzle_volume_type,
        extruder_id: body.extruder_id,
        filaments: body.filaments,
      }),
    onSuccess: (session) => {
      setSessionId(session.id);
      setStep('running');
      setErrorMsg(null);
      qc.invalidateQueries({ queryKey: ['calibration', 'awaiting', printerId] });
    },
    onError: (e: Error) => setErrorMsg(e.message),
  });

  // Submit manual result
  const submitManualMutation = useMutation({
    mutationFn: (body: ManualResultIn) => {
      if (sessionId == null) throw new Error('No active session');
      return api.submitManualResult(sessionId, body);
    },
    onSuccess: (out) => {
      if (out.next_session_id != null) {
        setSessionId(out.next_session_id);
        setStep('running');
      } else {
        setSavedRows(out.saved_rows);
        setStep('finish');
      }
      qc.invalidateQueries({ queryKey: ['filament-calibrations'] });
      qc.invalidateQueries({ queryKey: ['calibration', 'awaiting', printerId] });
    },
    onError: (e: Error) => setErrorMsg(e.message),
  });

  // Cancel
  const cancelMutation = useMutation({
    mutationFn: () => (sessionId != null ? api.cancelCalibrationSession(sessionId) : Promise.resolve()),
    onSuccess: () => {
      setSessionId(null);
      setStep('start');
      qc.invalidateQueries({ queryKey: ['calibration', 'awaiting', printerId] });
    },
    onError: (e: Error) => setErrorMsg(e.message),
  });

  // WS auto-advance: listen for calibration.completed on this session
  useEffect(() => {
    if (sessionId == null) return;
    const handler = (e: Event) => {
      const ce = e as CustomEvent<{ type: string; data?: Record<string, unknown> }>;
      const detail = ce.detail;
      const sid = detail?.data?.session_id;
      if (sid !== sessionId) return;
      if (detail.type === 'calibration.completed') {
        // Trigger refetch of session — next render flips status → wizard step
        qc.invalidateQueries({ queryKey: ['calibration', 'session', sessionId] });
      }
      if (detail.type === 'calibration.failed') {
        setErrorMsg((detail.data?.error as string) ?? 'Calibration failed');
      }
    };
    window.addEventListener('calibration-event', handler);
    return () => window.removeEventListener('calibration-event', handler);
  }, [sessionId, qc]);

  // When session reaches awaiting_user_input, advance step automatically
  useEffect(() => {
    if (sessionQuery.data?.status !== 'awaiting_user_input') return;
    if (step !== 'running') return;
    setStep(
      computeNextStep('running', {
        cali_mode: input.cali_mode,
        method: input.method,
        stage: sessionQuery.data.stage,
        sessionStatus: sessionQuery.data.status,
      }),
    );
  }, [sessionQuery.data, step, input.cali_mode, input.method]);

  const session = sessionQuery.data;

  return useMemo(
    () => ({
      step, setStep,
      input, setInput: (patch: Partial<WizardInput>) => setInput((p) => ({ ...p, ...patch })),
      capabilities: capQuery.data,
      awaitingSession: awaitingQuery.data?.[0] ?? null,
      session,
      sessionId,
      setSessionId,
      savedRows,
      errorMsg,
      isStarting: startMutation.isPending,
      isSubmitting: submitManualMutation.isPending,
      startSession: (body: WizardInput) => startMutation.mutateAsync(body),
      submitManualResult: (body: ManualResultIn) => submitManualMutation.mutateAsync(body),
      cancelSession: () => cancelMutation.mutateAsync(),
    }),
    [
      step, input, capQuery.data, awaitingQuery.data, session, sessionId, savedRows,
      errorMsg, startMutation, submitManualMutation, cancelMutation,
    ],
  );
}
```

- [ ] **Step 4: Run hook test**

Run: `cd frontend && npm run test:run -- src/__tests__/hooks/useFilamentCalibration.test.tsx`
Expected: 8 passed.

- [ ] **Step 5: Stage**

`git add frontend/src/hooks/useFilamentCalibration.ts frontend/src/__tests__/hooks/useFilamentCalibration.test.tsx`

---

### Task 7: Wizard shell — `FilamentCalibrationModal`

**Files:**
- Modify: `frontend/src/components/FilamentCalibrationModal.tsx` (replace stub)

- [ ] **Step 1: Test for shell render**

```typescript
// frontend/src/__tests__/components/FilamentCalibrationModal.test.tsx
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { FilamentCalibrationModal } from '../../components/FilamentCalibrationModal';

vi.mock('../../api/client', () => ({
  api: {
    getCalibrationCapabilities: vi.fn().mockResolvedValue({
      pa_manual: true, flow_manual: true,
      temp_tower: true, vol_speed_tower: true, vfa_tower: true, retraction_tower: true,
      pa_auto: false, flow_auto: false,
      dual_extruder: false,
      extruders: [{ id: 0, name: 'Main' }],
      nozzles: [{ id: 0, diameter: 0.4, type: 'stainless_steel', flow_type: 'standard' }],
    }),
    listAwaitingSessions: vi.fn().mockResolvedValue([]),
  },
}));

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

describe('FilamentCalibrationModal', () => {
  it('renders title and close button when open', () => {
    render(wrap(<FilamentCalibrationModal isOpen printerId={1} onClose={() => {}} />));
    expect(screen.getByText(/Filament Calibration|Калібровка філаменту/)).toBeInTheDocument();
    expect(screen.getByLabelText('Close')).toBeInTheDocument();
  });

  it('renders Start page initially', async () => {
    render(wrap(<FilamentCalibrationModal isOpen printerId={1} onClose={() => {}} />));
    expect(await screen.findByText(/PA Line|Pressure Advance/)).toBeInTheDocument();
  });

  it('returns null when closed', () => {
    const { container } = render(wrap(<FilamentCalibrationModal isOpen={false} printerId={1} onClose={() => {}} />));
    expect(container.firstChild).toBeNull();
  });
});
```

- [ ] **Step 2: Write the shell**

```tsx
// frontend/src/components/FilamentCalibrationModal.tsx
import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';

import { useToast } from '../contexts/ToastContext';
import { useFilamentCalibration } from '../hooks/useFilamentCalibration';
import { CalibrationStartPage } from './calibration/CalibrationStartPage';
import { CalibrationPresetPage } from './calibration/CalibrationPresetPage';
import { CalibrationRunningPage } from './calibration/CalibrationRunningPage';
import { CalibrationManualSavePage } from './calibration/CalibrationManualSavePage';
import { CalibrationCoarseSavePage } from './calibration/CalibrationCoarseSavePage';
import { CalibrationFineSavePage } from './calibration/CalibrationFineSavePage';
import { CalibrationFinishPage } from './calibration/CalibrationFinishPage';
import { ResumeBanner } from './calibration/ResumeBanner';

interface Props {
  isOpen: boolean;
  onClose: () => void;
  printerId: number;
}

export function FilamentCalibrationModal({ isOpen, onClose, printerId }: Props) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const cali = useFilamentCalibration(printerId, isOpen);

  // Surface backend errors as toasts
  useEffect(() => {
    if (cali.errorMsg) showToast(cali.errorMsg, 'error');
  }, [cali.errorMsg, showToast]);

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-2xl w-full max-w-3xl mx-4 max-h-[90vh] overflow-y-auto">
        <div className="flex justify-between items-center p-4 border-b border-bambu-dark-tertiary">
          <h2 className="text-lg font-semibold text-white">{t('filamentCali.title')}</h2>
          <button onClick={onClose} aria-label="Close" className="p-1 text-bambu-gray hover:text-white rounded transition-colors">
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="p-4 space-y-4">
          {cali.awaitingSession && cali.step === 'start' && (
            <ResumeBanner
              session={cali.awaitingSession}
              onResume={() => {
                cali.setSessionId(cali.awaitingSession!.id);
                // session query effect will advance step
                cali.setStep('running');
              }}
              onDiscard={async () => {
                cali.setSessionId(cali.awaitingSession!.id);
                await cali.cancelSession();
              }}
            />
          )}

          {cali.step === 'start' && (
            <CalibrationStartPage
              capabilities={cali.capabilities}
              onPick={(mode, method) => {
                cali.setInput({ cali_mode: mode, method });
                cali.setStep('preset');
              }}
            />
          )}

          {cali.step === 'preset' && cali.input.cali_mode && (
            <CalibrationPresetPage
              printerId={printerId}
              caliMode={cali.input.cali_mode}
              method={cali.input.method ?? 'manual'}
              capabilities={cali.capabilities}
              onBack={() => cali.setStep('start')}
              onStart={async (preset) => {
                cali.setInput({
                  nozzle_diameter: preset.nozzle_diameter,
                  nozzle_volume_type: preset.nozzle_volume_type,
                  extruder_id: preset.extruder_id,
                  filaments: preset.filaments,
                });
                await cali.startSession({
                  cali_mode: cali.input.cali_mode!,
                  method: cali.input.method ?? 'manual',
                  nozzle_diameter: preset.nozzle_diameter,
                  nozzle_volume_type: preset.nozzle_volume_type,
                  extruder_id: preset.extruder_id,
                  filaments: preset.filaments,
                });
              }}
            />
          )}

          {cali.step === 'running' && cali.session && (
            <CalibrationRunningPage
              session={cali.session}
              onCancel={() => cali.cancelSession()}
            />
          )}

          {cali.step === 'manualSave' && cali.session && (
            <CalibrationManualSavePage
              session={cali.session}
              onSave={(body) => cali.submitManualResult(body)}
              onBack={() => cali.setStep('running')}
              isSubmitting={cali.isSubmitting}
            />
          )}

          {cali.step === 'coarseSave' && cali.session && (
            <CalibrationCoarseSavePage
              session={cali.session}
              onSubmit={(body) => cali.submitManualResult(body)}
              isSubmitting={cali.isSubmitting}
            />
          )}

          {cali.step === 'fineSave' && cali.session && (
            <CalibrationFineSavePage
              session={cali.session}
              onSubmit={(body) => cali.submitManualResult(body)}
              isSubmitting={cali.isSubmitting}
            />
          )}

          {cali.step === 'finish' && (
            <CalibrationFinishPage
              savedRows={cali.savedRows}
              onCalibrateAnother={() => {
                cali.setSessionId(null);
                cali.setStep('start');
              }}
              onClose={onClose}
            />
          )}
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Create stub sub-components so build passes**

For now, scaffold each sub-component as a stub (they'll be replaced in Waves 3-5):

```tsx
// frontend/src/components/calibration/CalibrationStartPage.tsx
import type { CalibCapabilities } from '../../api/client';
import type { CaliMethod, CaliMode } from '../../api/client';
interface Props {
  capabilities: CalibCapabilities | undefined;
  onPick: (mode: CaliMode, method: CaliMethod) => void;
}
export function CalibrationStartPage(_: Props) { return <div>StartPage (stub)</div>; }
```

Similar 1-line stubs for `CalibrationPresetPage`, `CalibrationRunningPage`, `CalibrationManualSavePage`, `CalibrationCoarseSavePage`, `CalibrationFineSavePage`, `CalibrationFinishPage`, `ResumeBanner` — each receiving the props they use in the shell above, returning `<div>X (stub)</div>`.

Create all under `frontend/src/components/calibration/`.

- [ ] **Step 4: Type check + run shell test**

```
cd frontend && npx tsc --noEmit
npm run test:run -- src/__tests__/components/FilamentCalibrationModal.test.tsx
```

Expected: tsc clean, 3 tests passed.

- [ ] **Step 5: Stage**

`git add frontend/src/components/FilamentCalibrationModal.tsx frontend/src/components/calibration/ frontend/src/__tests__/components/FilamentCalibrationModal.test.tsx`

---

### Wave 2 verify

- [ ] Tests

```
pytest backend/tests/unit/services/test_calibration_service.py -v
cd frontend && npm run test:run -- src/__tests__/hooks/useFilamentCalibration.test.tsx src/__tests__/components/FilamentCalibrationModal.test.tsx
```

Expected: all passed.

- [ ] Wave 2 commit (when user asks):

```
feat(calibration): WS events + wizard hook + shell modal (Plan 2 Wave 2)

Backend: broadcast_calibration_event helper emits started/saved/
completed/cancelled/failed for the wizard frontend. Wired into
CalibrationService + background_dispatch on-complete hook.

Frontend:
- useFilamentCalibration hook (state machine + WS auto-advance).
- FilamentCalibrationModal shell — 7 stub sub-pages, ResumeBanner.
- useWebSocket routes calibration.* messages to a CustomEvent +
  query invalidation.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Wave 3 — Start + Preset + Running pages

### Task 8: `CalibrationStartPage` — mode picker

**Files:**
- Modify: `frontend/src/components/calibration/CalibrationStartPage.tsx` (replace stub)
- Test: `frontend/src/__tests__/components/CalibrationStartPage.test.tsx`

- [ ] **Step 1: Test**

```typescript
// frontend/src/__tests__/components/CalibrationStartPage.test.tsx
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { CalibrationStartPage } from '../../components/calibration/CalibrationStartPage';
import type { CalibCapabilities } from '../../api/client';

const caps: CalibCapabilities = {
  pa_manual: true, flow_manual: true,
  temp_tower: true, vol_speed_tower: true, vfa_tower: true, retraction_tower: true,
  pa_auto: false, flow_auto: false,
  dual_extruder: false,
  extruders: [{ id: 0, name: 'Main' }],
  nozzles: [{ id: 0, diameter: 0.4, type: 'stainless_steel', flow_type: 'standard' }],
};

describe('CalibrationStartPage', () => {
  it('PA Line is enabled, calls onPick with manual', async () => {
    const onPick = vi.fn();
    render(<CalibrationStartPage capabilities={caps} onPick={onPick} />);
    const paLine = await screen.findByRole('button', { name: /PA Line/i });
    await userEvent.click(paLine);
    expect(onPick).toHaveBeenCalledWith('pa_line', 'manual');
  });

  it('PA Auto disabled when capability false', () => {
    render(<CalibrationStartPage capabilities={caps} onPick={() => {}} />);
    const autoBtn = screen.getByRole('button', { name: /Auto PA/i });
    expect(autoBtn).toBeDisabled();
  });

  it('Tower modes disabled with Plan 3 hint', () => {
    render(<CalibrationStartPage capabilities={caps} onPick={() => {}} />);
    const towerBtns = screen.getAllByText(/Plan 3/i);
    expect(towerBtns.length).toBeGreaterThan(0);
  });
});
```

- [ ] **Step 2: Run test, fail**

Run: `cd frontend && npm run test:run -- src/__tests__/components/CalibrationStartPage.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Write the component**

```tsx
// frontend/src/components/calibration/CalibrationStartPage.tsx
import { useTranslation } from 'react-i18next';
import type { CalibCapabilities, CaliMethod, CaliMode } from '../../api/client';

interface Props {
  capabilities: CalibCapabilities | undefined;
  onPick: (mode: CaliMode, method: CaliMethod) => void;
}

interface OptionRow {
  mode: CaliMode;
  method: CaliMethod;
  labelKey: string;
  descKey: string;
  capKey: keyof CalibCapabilities;
  comingPlan3?: boolean;
}

const PA_OPTIONS: OptionRow[] = [
  { mode: 'pa_line',    method: 'manual', labelKey: 'paLine',    descKey: 'paLineDesc',    capKey: 'pa_manual' },
  { mode: 'pa_pattern', method: 'manual', labelKey: 'paPattern', descKey: 'paPatternDesc', capKey: 'pa_manual', comingPlan3: true },
  { mode: 'pa_tower',   method: 'manual', labelKey: 'paTower',   descKey: 'paTowerDesc',   capKey: 'pa_manual', comingPlan3: true },
  { mode: 'auto_pa_line', method: 'auto', labelKey: 'paAuto',    descKey: 'paAutoDesc',    capKey: 'pa_auto',   comingPlan3: true },
];

const FLOW_OPTIONS: OptionRow[] = [
  { mode: 'flow_rate', method: 'manual', labelKey: 'flowRate', descKey: 'flowRateDesc', capKey: 'flow_manual' },
  { mode: 'flow_rate', method: 'auto',   labelKey: 'flowAuto', descKey: 'flowAutoDesc', capKey: 'flow_auto',   comingPlan3: true },
];

const TOWER_OPTIONS: OptionRow[] = [
  { mode: 'temp_tower',      method: 'manual', labelKey: 'paLine', descKey: 'paLineDesc', capKey: 'temp_tower',     comingPlan3: true },
  { mode: 'vol_speed_tower', method: 'manual', labelKey: 'paLine', descKey: 'paLineDesc', capKey: 'vol_speed_tower',comingPlan3: true },
  { mode: 'vfa_tower',       method: 'manual', labelKey: 'paLine', descKey: 'paLineDesc', capKey: 'vfa_tower',      comingPlan3: true },
  { mode: 'retraction_tower',method: 'manual', labelKey: 'paLine', descKey: 'paLineDesc', capKey: 'retraction_tower',comingPlan3: true },
];

export function CalibrationStartPage({ capabilities, onPick }: Props) {
  const { t } = useTranslation();

  const renderRow = (r: OptionRow) => {
    const supported = capabilities ? Boolean(capabilities[r.capKey]) : false;
    const disabled = !supported || r.comingPlan3;
    const reason = r.comingPlan3
      ? t('filamentCali.start.comingInPlan3')
      : !supported
      ? t('filamentCali.start.notSupported')
      : '';
    return (
      <button
        key={`${r.mode}-${r.method}`}
        type="button"
        onClick={() => !disabled && onPick(r.mode, r.method)}
        disabled={disabled}
        title={reason}
        className={`w-full text-left p-3 rounded-lg border transition-colors ${
          disabled
            ? 'border-bambu-dark-tertiary bg-bambu-dark opacity-50 cursor-not-allowed'
            : 'border-bambu-dark-tertiary bg-bambu-dark hover:border-bambu-green'
        }`}
      >
        <div className="flex items-center justify-between">
          <span className="font-medium text-white">{t(`filamentCali.start.${r.labelKey}`)}</span>
          {disabled && <span className="text-xs text-bambu-gray">{reason}</span>}
        </div>
        <p className="text-sm text-bambu-gray mt-1">{t(`filamentCali.start.${r.descKey}`)}</p>
      </button>
    );
  };

  return (
    <div className="space-y-4">
      <h3 className="text-base font-semibold text-white">{t('filamentCali.start.heading')}</h3>

      <section>
        <h4 className="text-sm font-medium text-bambu-gray mb-2">{t('filamentCali.start.paGroup')}</h4>
        <div className="space-y-2">{PA_OPTIONS.map(renderRow)}</div>
      </section>

      <section>
        <h4 className="text-sm font-medium text-bambu-gray mb-2">{t('filamentCali.start.flowGroup')}</h4>
        <div className="space-y-2">{FLOW_OPTIONS.map(renderRow)}</div>
      </section>

      <section>
        <h4 className="text-sm font-medium text-bambu-gray mb-2">{t('filamentCali.start.towerGroup')}</h4>
        <div className="space-y-2">{TOWER_OPTIONS.map(renderRow)}</div>
      </section>
    </div>
  );
}
```

- [ ] **Step 4: Run tests**

Run: `cd frontend && npm run test:run -- src/__tests__/components/CalibrationStartPage.test.tsx`
Expected: 3 passed.

- [ ] **Step 5: Stage**

`git add frontend/src/components/calibration/CalibrationStartPage.tsx frontend/src/__tests__/components/CalibrationStartPage.test.tsx`

---

### Task 9: `CalibrationPresetPage` — filament + preset inputs

**Files:**
- Modify: `frontend/src/components/calibration/CalibrationPresetPage.tsx`

- [ ] **Step 1: Find existing spool / AMS data source**

We need: list of currently-loaded AMS slots with `(ams_id, slot_id, tray_id, filament_id)`. Look for an existing hook:

Grep: `grep -rln "filament_id\|ams_id" frontend/src/hooks/ frontend/src/api/client.ts | head -10`

Use whatever the printer card uses to display its AMS slots (e.g. `useAmsState(printerId)` or printer-status WS data). Snapshot the API shape with `grep -n "ams_id" frontend/src/types/*.ts` and reuse.

- [ ] **Step 2: Write component**

```tsx
// frontend/src/components/calibration/CalibrationPresetPage.tsx
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import type {
  CalibCapabilities, CaliMethod, CaliMode, CalibFilamentIn, NozzleVolumeType,
} from '../../api/client';
// Substitute the correct printer-state hook used by the printer card:
import { usePrinterState } from '../../hooks/usePrinterState';

interface Props {
  printerId: number;
  caliMode: CaliMode;
  method: CaliMethod;
  capabilities: CalibCapabilities | undefined;
  onBack: () => void;
  onStart: (preset: {
    nozzle_diameter: number;
    nozzle_volume_type: NozzleVolumeType;
    extruder_id: number;
    filaments: CalibFilamentIn[];
  }) => Promise<void>;
}

export function CalibrationPresetPage({
  printerId, caliMode, method, capabilities, onBack, onStart,
}: Props) {
  const { t } = useTranslation();
  const state = usePrinterState(printerId);

  // Default to nozzle 0 from capabilities
  const firstNozzleDia = capabilities?.nozzles?.[0]?.diameter ?? 0.4;
  const [nozzleDia, setNozzleDia] = useState<number>(firstNozzleDia);
  const [nozzleVolType, setNozzleVolType] = useState<NozzleVolumeType>('standard');
  const [bedTemp, setBedTemp] = useState<number>(60);
  const [nozzleTemp, setNozzleTemp] = useState<number>(220);
  const [maxVolSpeed, setMaxVolSpeed] = useState<number>(12);
  const [selectedSlot, setSelectedSlot] = useState<{
    ams_id: number; slot_id: number; tray_id: number;
    filament_id: string; filament_setting_id: string | null;
  } | null>(null);

  // Filter loaded AMS slots (adapt to actual state.ams_slots shape)
  const loadedSlots = (state?.ams_slots ?? [])
    .filter((s) => s.filament_id && s.filament_id !== '')
    .map((s) => ({
      ams_id: s.ams_id ?? 0,
      slot_id: s.slot_id ?? 0,
      tray_id: s.tray_id ?? (s.ams_id ?? 0) * 4 + (s.slot_id ?? 0),
      filament_id: s.filament_id,
      filament_setting_id: s.setting_id ?? null,
      label: `AMS ${(s.ams_id ?? 0) + 1} · Slot ${(s.slot_id ?? 0) + 1} · ${s.filament_id}`,
    }));

  const canStart =
    selectedSlot != null &&
    bedTemp > 0 &&
    nozzleTemp > 0 &&
    maxVolSpeed > 0;

  const submit = async () => {
    if (!selectedSlot) return;
    await onStart({
      nozzle_diameter: nozzleDia,
      nozzle_volume_type: nozzleVolType,
      extruder_id: 0, // dual-extruder UI in Plan 3
      filaments: [
        {
          ams_id: selectedSlot.ams_id,
          slot_id: selectedSlot.slot_id,
          tray_id: selectedSlot.tray_id,
          filament_id: selectedSlot.filament_id,
          filament_setting_id: selectedSlot.filament_setting_id,
          bed_temp: bedTemp,
          nozzle_temp: nozzleTemp,
          max_volumetric_speed: maxVolSpeed,
        },
      ],
    });
  };

  return (
    <div className="space-y-4">
      <h3 className="text-base font-semibold text-white">{t('filamentCali.preset.heading')}</h3>

      <section className="space-y-2">
        <div className="grid grid-cols-2 gap-2">
          <label className="block">
            <span className="text-xs text-bambu-gray">{t('filamentCali.preset.nozzleDia')}</span>
            <select
              value={nozzleDia}
              onChange={(e) => setNozzleDia(parseFloat(e.target.value))}
              className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
            >
              {capabilities?.nozzles?.map((n) => (
                <option key={n.id} value={n.diameter ?? 0.4}>{n.diameter ?? 0.4} mm</option>
              )) ?? <option value={0.4}>0.4 mm</option>}
            </select>
          </label>
          <label className="block">
            <span className="text-xs text-bambu-gray">{t('filamentCali.preset.nozzleType')}</span>
            <select
              value={nozzleVolType}
              onChange={(e) => setNozzleVolType(e.target.value as NozzleVolumeType)}
              className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
            >
              <option value="standard">Standard</option>
              <option value="high_flow">High Flow</option>
              <option value="tpu_high_flow">TPU High Flow</option>
              <option value="hybrid">Hybrid</option>
            </select>
          </label>
        </div>
      </section>

      <section>
        <span className="text-sm font-medium text-bambu-gray block mb-2">{t('filamentCali.preset.selectFilament')}</span>
        {loadedSlots.length === 0 ? (
          <div className="p-3 bg-bambu-dark rounded text-sm text-bambu-gray">
            {t('filamentCali.preset.noLoadedSlot')}
          </div>
        ) : (
          <div className="space-y-2">
            {loadedSlots.map((s) => (
              <button
                key={`${s.ams_id}-${s.slot_id}`}
                type="button"
                onClick={() => setSelectedSlot(s)}
                className={`w-full text-left p-2 rounded border ${
                  selectedSlot?.ams_id === s.ams_id && selectedSlot.slot_id === s.slot_id
                    ? 'border-bambu-green bg-bambu-green/10'
                    : 'border-bambu-dark-tertiary bg-bambu-dark hover:border-bambu-green/50'
                }`}
              >
                <span className="text-white">{s.label}</span>
              </button>
            ))}
          </div>
        )}
      </section>

      <section className="grid grid-cols-3 gap-2">
        <label className="block">
          <span className="text-xs text-bambu-gray">{t('filamentCali.preset.bedTemp')}</span>
          <input
            type="number"
            value={bedTemp}
            onChange={(e) => setBedTemp(parseInt(e.target.value, 10) || 0)}
            className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
          />
        </label>
        <label className="block">
          <span className="text-xs text-bambu-gray">{t('filamentCali.preset.nozzleTemp')}</span>
          <input
            type="number"
            value={nozzleTemp}
            onChange={(e) => setNozzleTemp(parseInt(e.target.value, 10) || 0)}
            className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
          />
        </label>
        <label className="block">
          <span className="text-xs text-bambu-gray">{t('filamentCali.preset.maxVolSpeed')}</span>
          <input
            type="number"
            step="0.5"
            value={maxVolSpeed}
            onChange={(e) => setMaxVolSpeed(parseFloat(e.target.value) || 0)}
            className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
          />
        </label>
      </section>

      <div className="flex justify-between pt-2 border-t border-bambu-dark-tertiary">
        <button type="button" onClick={onBack} className="px-3 py-1.5 text-sm text-bambu-gray hover:text-white">
          {t('filamentCali.back')}
        </button>
        <button
          type="button"
          onClick={submit}
          disabled={!canStart}
          className="px-4 py-2 rounded bg-bambu-green text-white text-sm font-medium disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {t('filamentCali.startCalibration')}
        </button>
      </div>
    </div>
  );
}
```

(Adapt `usePrinterState` to your actual hook + `state.ams_slots` field name.)

- [ ] **Step 2.1: Verify the hook name**

Grep: `grep -rln "usePrinterState\|usePrinter(" frontend/src/hooks/`. If a different hook exposes AMS slot info, swap import.

- [ ] **Step 3: Type check**

Run: `cd frontend && npx tsc --noEmit`
Expected: clean (or one fix-up if hook name differs).

- [ ] **Step 4: Stage**

`git add frontend/src/components/calibration/CalibrationPresetPage.tsx`

---

### Task 10: `CalibrationRunningPage` — live print progress

**Files:**
- Modify: `frontend/src/components/calibration/CalibrationRunningPage.tsx`

- [ ] **Step 1: Find existing live-progress widget**

Grep: `grep -rln "stg_cur\|stage.*progress\|PrintProgress" frontend/src/components/ | head -5`. Reuse if there's an existing `<PrintProgress>` component.

- [ ] **Step 2: Component**

```tsx
// frontend/src/components/calibration/CalibrationRunningPage.tsx
import { useTranslation } from 'react-i18next';
import type { CalibrationSessionOut } from '../../api/client';
// Reuse existing printer status (substitute correct hook name if different):
import { usePrinterState } from '../../hooks/usePrinterState';

interface Props {
  session: CalibrationSessionOut;
  onCancel: () => Promise<void> | void;
}

export function CalibrationRunningPage({ session, onCancel }: Props) {
  const { t } = useTranslation();
  const state = usePrinterState(session.printer_id);

  const progress = state?.mc_percent ?? 0;
  const stage = state?.stg_cur ?? -1;
  const layer = state?.layer_num ?? null;
  const totalLayers = state?.total_layer_num ?? null;

  const handleCancel = async () => {
    if (window.confirm(t('filamentCali.running.cancelConfirm'))) {
      await onCancel();
    }
  };

  return (
    <div className="space-y-4">
      <h3 className="text-base font-semibold text-white">{t('filamentCali.running.heading')}</h3>

      <div className="p-3 bg-bambu-dark rounded border border-bambu-dark-tertiary space-y-2">
        <div className="flex justify-between text-sm">
          <span className="text-bambu-gray">{session.cali_mode}</span>
          <span className="text-white font-medium">{progress}%</span>
        </div>
        <div className="h-2 bg-bambu-dark-tertiary rounded overflow-hidden">
          <div className="h-full bg-bambu-green transition-all" style={{ width: `${progress}%` }} />
        </div>
        {layer != null && totalLayers != null && (
          <div className="text-xs text-bambu-gray">Layer {layer} / {totalLayers}</div>
        )}
        {stage >= 0 && <div className="text-xs text-bambu-gray">Stage code: {stage}</div>}
      </div>

      <p className="text-sm text-bambu-gray">{t('filamentCali.running.inProgress')}</p>

      <div className="flex justify-end pt-2 border-t border-bambu-dark-tertiary">
        <button
          type="button"
          onClick={handleCancel}
          className="px-3 py-1.5 text-sm text-red-400 hover:text-red-300"
        >
          {t('filamentCali.running.cancel')}
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Type check**

Run: `cd frontend && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 4: Stage**

`git add frontend/src/components/calibration/CalibrationRunningPage.tsx`

---

### Wave 3 verify

- [ ] Tests + build

```
cd frontend && npm run test:run -- src/__tests__/components/CalibrationStartPage.test.tsx
npx tsc --noEmit
npm run lint
```

Expected: all green.

- [ ] Wave 3 commit suggestion:

```
feat(calibration): Start / Preset / Running pages (Plan 2 Wave 3)

- StartPage: mode picker with capability + Plan 3 gating.
- PresetPage: nozzle + filament-slot + temps + max vol speed inputs.
  Extruder 0 hard-coded for Plan 2 (Plan 3 adds H2D dual UI).
- RunningPage: progress bar + stage code + Cancel.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Wave 4 — Save pages

### Task 11: `CalibrationManualSavePage` (PA Line)

**Files:**
- Modify: `frontend/src/components/calibration/CalibrationManualSavePage.tsx`

- [ ] **Step 1: Component**

```tsx
// frontend/src/components/calibration/CalibrationManualSavePage.tsx
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { CalibrationSessionOut, ManualResultIn } from '../../api/client';

// PA Line range — must match backend calibration_constants.PA_LINE_RANGE
const PA_LINE_RANGE = { start: 0.0, step: 0.002, count: 50 };

interface Props {
  session: CalibrationSessionOut;
  onSave: (body: ManualResultIn) => Promise<unknown>;
  onBack: () => void;
  isSubmitting: boolean;
}

export function CalibrationManualSavePage({ session, onSave, onBack, isSubmitting }: Props) {
  const { t } = useTranslation();
  const [lineIdx, setLineIdx] = useState<number>(Math.floor(PA_LINE_RANGE.count / 2));

  const k = PA_LINE_RANGE.start + lineIdx * PA_LINE_RANGE.step;

  return (
    <div className="space-y-4">
      <h3 className="text-base font-semibold text-white">{t('filamentCali.manualSave.heading')}</h3>
      <p className="text-sm text-bambu-gray">{t('filamentCali.manualSave.instruction')}</p>

      <label className="block">
        <span className="text-xs text-bambu-gray">{t('filamentCali.manualSave.lineIndex')}</span>
        <select
          value={lineIdx}
          onChange={(e) => setLineIdx(parseInt(e.target.value, 10))}
          className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
        >
          {Array.from({ length: PA_LINE_RANGE.count }, (_, i) => (
            <option key={i} value={i}>
              {i} (PA {(PA_LINE_RANGE.start + i * PA_LINE_RANGE.step).toFixed(4)})
            </option>
          ))}
        </select>
      </label>

      <div className="p-2 bg-bambu-dark rounded text-sm">
        <span className="text-bambu-gray">{t('filamentCali.manualSave.computedK')}: </span>
        <span className="text-white font-mono">{k.toFixed(4)}</span>
      </div>

      <div className="flex justify-between pt-2 border-t border-bambu-dark-tertiary">
        <button type="button" onClick={onBack} className="px-3 py-1.5 text-sm text-bambu-gray hover:text-white">
          {t('filamentCali.back')}
        </button>
        <button
          type="button"
          onClick={() => onSave({ best_line_index: lineIdx })}
          disabled={isSubmitting}
          className="px-4 py-2 rounded bg-bambu-green text-white text-sm font-medium disabled:opacity-40"
        >
          {t('filamentCali.manualSave.save')}
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

`git add frontend/src/components/calibration/CalibrationManualSavePage.tsx`

---

### Task 12: `CalibrationCoarseSavePage` (Flow Rate stage 1)

**Files:**
- Modify: `frontend/src/components/calibration/CalibrationCoarseSavePage.tsx`

- [ ] **Step 1: Component**

```tsx
// frontend/src/components/calibration/CalibrationCoarseSavePage.tsx
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { CalibrationSessionOut, ManualResultIn } from '../../api/client';

// Must match backend FLOW_RATE_COARSE_MODIFIERS
const COARSE_MODS = [-20, -15, -10, -5, 0, 5, 10, 15, 20];

interface Props {
  session: CalibrationSessionOut;
  onSubmit: (body: ManualResultIn) => Promise<unknown>;
  isSubmitting: boolean;
}

export function CalibrationCoarseSavePage({ session, onSubmit, isSubmitting }: Props) {
  const { t } = useTranslation();
  const [mod, setMod] = useState<number>(0);
  const [skipFine, setSkipFine] = useState<boolean>(false);

  const coarseRatio = (100 + mod) / 100;

  return (
    <div className="space-y-4">
      <h3 className="text-base font-semibold text-white">{t('filamentCali.coarseSave.heading')}</h3>
      <p className="text-sm text-bambu-gray">{t('filamentCali.coarseSave.instruction')}</p>

      <label className="block">
        <span className="text-xs text-bambu-gray">{t('filamentCali.coarseSave.blockModifier')}</span>
        <select
          value={mod}
          onChange={(e) => setMod(parseInt(e.target.value, 10))}
          className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
        >
          {COARSE_MODS.map((m) => (
            <option key={m} value={m}>{m > 0 ? `+${m}%` : `${m}%`}</option>
          ))}
        </select>
      </label>

      <div className="p-2 bg-bambu-dark rounded text-sm">
        <span className="text-bambu-gray">{t('filamentCali.coarseSave.coarseRatio')}: </span>
        <span className="text-white font-mono">{coarseRatio.toFixed(4)}</span>
      </div>

      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={skipFine}
          onChange={(e) => setSkipFine(e.target.checked)}
          className="rounded"
        />
        <span className="text-bambu-gray">{t('filamentCali.coarseSave.skipFine')}</span>
      </label>

      <div className="flex justify-end pt-2 border-t border-bambu-dark-tertiary">
        <button
          type="button"
          onClick={() => onSubmit({ coarse_modifier: mod, skip_fine: skipFine })}
          disabled={isSubmitting}
          className="px-4 py-2 rounded bg-bambu-green text-white text-sm font-medium disabled:opacity-40"
        >
          {skipFine ? t('filamentCali.coarseSave.saveAndFinish') : t('filamentCali.coarseSave.continue')}
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

`git add frontend/src/components/calibration/CalibrationCoarseSavePage.tsx`

---

### Task 13: `CalibrationFineSavePage` (Flow Rate stage 2)

**Files:**
- Modify: `frontend/src/components/calibration/CalibrationFineSavePage.tsx`

- [ ] **Step 1: Component**

```tsx
// frontend/src/components/calibration/CalibrationFineSavePage.tsx
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { CalibrationSessionOut, ManualResultIn } from '../../api/client';

const FINE_MODS = [-5, -2, 0, 2, 5, 10, 15];

interface Props {
  session: CalibrationSessionOut;
  onSubmit: (body: ManualResultIn) => Promise<unknown>;
  isSubmitting: boolean;
}

export function CalibrationFineSavePage({ session, onSubmit, isSubmitting }: Props) {
  const { t } = useTranslation();
  const [mod, setMod] = useState<number>(0);

  const coarse = session.coarse_ratio ?? 1.0;
  const fine = coarse * (100 + mod) / 100;

  return (
    <div className="space-y-4">
      <h3 className="text-base font-semibold text-white">{t('filamentCali.fineSave.heading')}</h3>
      <p className="text-sm text-bambu-gray">{t('filamentCali.fineSave.instruction')}</p>

      <label className="block">
        <span className="text-xs text-bambu-gray">{t('filamentCali.fineSave.fineModifier')}</span>
        <select
          value={mod}
          onChange={(e) => setMod(parseInt(e.target.value, 10))}
          className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
        >
          {FINE_MODS.map((m) => (
            <option key={m} value={m}>{m > 0 ? `+${m}%` : `${m}%`}</option>
          ))}
        </select>
      </label>

      <div className="p-2 bg-bambu-dark rounded text-sm">
        <span className="text-bambu-gray">{t('filamentCali.fineSave.fineRatio')}: </span>
        <span className="text-white font-mono">{fine.toFixed(4)}</span>
      </div>

      <div className="flex justify-end pt-2 border-t border-bambu-dark-tertiary">
        <button
          type="button"
          onClick={() => onSubmit({ fine_modifier: mod })}
          disabled={isSubmitting}
          className="px-4 py-2 rounded bg-bambu-green text-white text-sm font-medium disabled:opacity-40"
        >
          {t('filamentCali.fineSave.save')}
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

`git add frontend/src/components/calibration/CalibrationFineSavePage.tsx`

---

### Task 14: `CalibrationFinishPage`

**Files:**
- Modify: `frontend/src/components/calibration/CalibrationFinishPage.tsx`

- [ ] **Step 1: Component**

```tsx
// frontend/src/components/calibration/CalibrationFinishPage.tsx
import { useTranslation } from 'react-i18next';
import { CheckCircle2 } from 'lucide-react';
import type { FilamentCalibrationOut } from '../../api/client';

interface Props {
  savedRows: FilamentCalibrationOut[];
  onCalibrateAnother: () => void;
  onClose: () => void;
}

export function CalibrationFinishPage({ savedRows, onCalibrateAnother, onClose }: Props) {
  const { t } = useTranslation();

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <CheckCircle2 className="h-6 w-6 text-bambu-green" />
        <h3 className="text-base font-semibold text-white">{t('filamentCali.finish.heading')}</h3>
      </div>

      <p className="text-sm text-bambu-gray">{t('filamentCali.finish.body')}</p>

      {savedRows.length > 0 && (
        <div className="space-y-1">
          {savedRows.map((r) => (
            <div key={r.id} className="p-2 bg-bambu-dark rounded text-sm flex justify-between">
              <span className="text-white">{r.name}</span>
              {r.pa_k_value != null && <span className="text-bambu-gray font-mono">K = {r.pa_k_value.toFixed(4)}</span>}
              {r.flow_ratio != null && <span className="text-bambu-gray font-mono">flow = {r.flow_ratio.toFixed(4)}</span>}
            </div>
          ))}
        </div>
      )}

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

- [ ] **Step 2: Type check + lint**

```
cd frontend && npx tsc --noEmit && npm run lint
```

Expected: clean.

- [ ] **Step 3: Stage**

`git add frontend/src/components/calibration/CalibrationFinishPage.tsx`

---

### Task 15: `ResumeBanner`

**Files:**
- Modify: `frontend/src/components/calibration/ResumeBanner.tsx`

- [ ] **Step 1: Component**

```tsx
// frontend/src/components/calibration/ResumeBanner.tsx
import { useTranslation } from 'react-i18next';
import { AlertCircle, X } from 'lucide-react';
import type { CalibrationSessionOut } from '../../api/client';

interface Props {
  session: CalibrationSessionOut;
  onResume: () => void;
  onDiscard: () => Promise<void> | void;
}

export function ResumeBanner({ session, onResume, onDiscard }: Props) {
  const { t } = useTranslation();
  const date = new Date(session.created_at).toLocaleString();

  const handleDiscard = async () => {
    if (window.confirm(t('filamentCali.resume.discardConfirm'))) {
      await onDiscard();
    }
  };

  const body =
    session.cali_mode === 'flow_rate'
      ? t('filamentCali.resume.bodyFlow', {
          filament: session.filaments_json ? '' : '',
          stage: session.stage === 2 ? 'fine' : 'coarse',
          date,
        })
      : t('filamentCali.resume.bodyPaLine', { filament: '', date });

  return (
    <div className="p-3 bg-bambu-dark-tertiary border border-yellow-700/50 rounded-lg flex items-start gap-3">
      <AlertCircle className="h-5 w-5 text-yellow-500 mt-0.5 shrink-0" />
      <div className="flex-1">
        <div className="text-sm font-medium text-white">{t('filamentCali.resume.title')}</div>
        <div className="text-xs text-bambu-gray mt-0.5">{body}</div>
        <div className="mt-2 flex gap-2">
          <button onClick={onResume} className="px-2 py-1 text-xs rounded bg-bambu-green text-white">
            {t('filamentCali.resume.resume')}
          </button>
          <button onClick={handleDiscard} className="px-2 py-1 text-xs rounded border border-bambu-dark-tertiary text-bambu-gray hover:text-white">
            {t('filamentCali.resume.discard')}
          </button>
        </div>
      </div>
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

`git add frontend/src/components/calibration/ResumeBanner.tsx`

---

### Wave 4 verify

- [ ] Test + build sweep

```
cd frontend && npm run test:run -- src/__tests__/components/FilamentCalibrationModal.test.tsx src/__tests__/components/CalibrationStartPage.test.tsx src/__tests__/hooks/useFilamentCalibration.test.tsx
npx tsc --noEmit
npm run lint
npm run build
```

Expected: all green, bundle under `static/` updates.

- [ ] Wave 4 commit suggestion:

```
feat(calibration): manual save pages + Finish + ResumeBanner (Plan 2 Wave 4)

- ManualSavePage: PA line picker with live K calc; range matches
  backend PA_LINE_RANGE.
- CoarseSavePage: Flow Rate stage 1 with 9-block dropdown + Skip Fine.
- FineSavePage: stage 2 refined modifier.
- FinishPage: confirmation with saved-row K/flow display.
- ResumeBanner: awaiting_user_input session badge with
  Resume / Discard actions.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Wave 5 — Manual end-to-end sanity + polish

### Task 16: Manual smoke walkthrough (instructed)

This task is human-executed, not a code change. Captures any plumbing gaps before declaring Plan 2 done.

- [ ] **Step 1: Start backend + frontend**

```
DEBUG=true uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000
cd frontend && npm run dev
```

- [ ] **Step 2: PA Line walkthrough**

In browser:

1. Open Printers page. Click kebab on online P1S → "Filament Calibration".
2. StartPage opens. Click **PA Line (manual)**. PresetPage opens.
3. Select an AMS slot with a loaded spool. Adjust temps if needed.
4. Click **Start calibration**.
5. RunningPage shows, progress bar advances.
6. When print finishes — wizard auto-advances to ManualSavePage (WS event arrives).
7. Pick a line index (e.g. 24). K = 0.048 shows.
8. Click **Save & finish** → FinishPage with the saved row.
9. Reopen wizard, click Start: confirm no resume banner appears (saved sessions don't show up).

Pause/abort: any step — close modal → reopen → see ResumeBanner pointing at the in-flight session.

- [ ] **Step 3: Flow Rate walkthrough**

Same as Step 2 but pick **Flow Rate (manual, coarse + fine)**:

1. Run pass 1 (9-block coarse print).
2. CoarseSavePage opens. Pick mod 5 (+5 %). Do NOT check "Skip fine".
3. Click **Continue to fine**. New session starts, RunningPage shows pass-2 print.
4. When done — FineSavePage opens.
5. Pick fine mod 2. Final flow ratio ≈ 1.071 shows.
6. Click **Save & finish**.

Skip-fine variant: redo from step 2 with "Skip Fine" toggled — should jump straight to FinishPage with flow_ratio=1.05.

- [ ] **Step 4: Cancel walkthrough**

Mid-running, click **Cancel calibration**. Confirm in dialog. Wizard returns to Start. Backend stops print via MQTT (verify in printer log).

- [ ] **Step 5: Document any gaps**

If any of the above breaks, capture details + suggested fix below before declaring Wave 5 done.

---

### Task 17: Lint sweep + bundle commit

- [ ] **Step 1: Full lint pass**

```
cd frontend && npm run lint
```

Auto-fix simple issues with `npm run lint -- --fix` if appropriate.

- [ ] **Step 2: Production build**

```
cd frontend && npm run build
```

Expected: bundle written under `static/`.

- [ ] **Step 3: Stage built bundle**

```
git add static/
```

- [ ] **Step 4: CHANGELOG entry**

Add one line under `[Unreleased]` in `CHANGELOG.md`:

```markdown
- Filament Calibration wizard (manual PA Line + Flow Rate). Kebab on printer card → guided wizard, results persist per filament + nozzle combination and auto-bind to the AMS slot.
```

`git add CHANGELOG.md`

- [ ] **Step 5: Plan 2 commit (when user asks):**

```
feat(calibration): Plan 2 — manual PA + Flow Rate UI complete

End-to-end manual calibration wizard for P1S / A1 / X1C-no-lidar:
- Mode picker (PA Line + Flow Rate visible; auto + towers gated to Plan 3).
- Filament-slot + nozzle + temps preset.
- Live running progress (re-uses printer status hook).
- Manual save pages: PA line picker, Flow coarse + fine combo-boxes,
  Skip Fine path.
- Finish page + Resume banner for interrupted sessions.
- en + uk i18n (filamentCali namespace).
- Frontend WS routes calibration.* events; wizard auto-advances on
  print completion via CustomEvent.

Backend: emits started / completed / saved / cancelled / failed WS
events from CalibrationService + dispatch.

Out of scope (Plan 3): Auto X1 save UI, Tower modes, full History
modal, H2D dual-extruder UI, docs site update.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Plan 2 Summary

**What this plan delivers:**
- 7-step manual wizard (Start → Preset → Running → ManualSave / CoarseSave → optional FineSave → Finish).
- Resume banner for awaiting_user_input sessions.
- Full TanStack Query 5 + WS auto-advance plumbing.
- en + uk i18n.
- Kebab menu entry on PrintersPage.
- Backend WS event emission for `calibration.started/completed/saved/cancelled/failed`.
- Production bundle ships under `static/`.

**What's NOT in this plan (Plan 3):**
- `CalibrationAutoSavePage` (X1 lidar pre-filled values).
- Tower modes (Temp / VolSpeed / VFA / Retraction).
- Full `CalibrationHistoryModal` (view + set-active + delete).
- H2D dual-extruder tabs in Preset page.
- 3MF assets actually copied from BS resources/ (Plan 1 left placeholders; Plan 2 walkthrough requires them).
- Docs site (`docs.bamdude.top`) + landing (`bamdude.top`) updates.

**Open follow-ups before Plan 2 ships to users:**
1. **Copy 3MF assets** from `temp/references/BambuStudio/resources/calib/` into `backend/app/data/calib_assets/`. Without these the manual path 404's on asset resolve.
2. **Confirm hook names** — `usePrinterState`, `usePrinterState.ams_slots`, `usePrinterState.mc_percent / stg_cur / layer_num` — substitute the actual ones in BamDude during Tasks 9 + 10.
3. **WS broadcaster** — `broadcast_to_printer` in Task 4 is a guess; verify the actual helper exposed by `backend/app/core/websocket.py` and adapt.
4. **PrintersPage kebab style** — match the wrapper component the file already uses (e.g. `KebabMenuItem`); if no such component, the inline button in Task 3 is fine.

---

## Spec Self-Review

**Coverage:**

| Spec section | Plan task |
|---|---|
| Frontend wizard shell | T7 |
| CalibrationStartPage (mode picker) | T8 |
| CalibrationPresetPage (filament + temps) | T9 |
| CalibrationRunningPage (live progress) | T10 |
| CalibrationManualSavePage (PA) | T11 |
| CalibrationCoarseSavePage + FineSavePage (Flow Rate) | T12, T13 |
| CalibrationFinishPage | T14 |
| ResumeBanner | T15 |
| WebSocket event emission backend + frontend route | T4, T5 |
| API client extensions | T1 |
| i18n en + uk | T2 |
| Kebab menu wire-up | T3 |
| useFilamentCalibration hook + state machine | T6 |

**Placeholder scan:** none. Tasks 9, 10, 4 explicitly flag "find actual hook/helper name" with a grep step before code change — substituted at impl time, not skipped.

**Type consistency:** `WizardStep` literals match between hook + shell. `ManualResultIn` / `ManualResultOut` shape consistent across api client + hook + save pages.

**Scope:** single deployable slice (manual-only UI). H2D dual-extruder UI and auto path explicitly deferred to Plan 3.
