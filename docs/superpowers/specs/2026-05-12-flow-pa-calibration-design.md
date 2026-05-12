# Flow Rate / PA Calibration — Design

**Status:** Spec  ·  **Date:** 2026-05-12  ·  **Goal:** повторити Bambu Studio `PressureAdvanceWizard` + `FlowRateWizard` функціонал у BamDude з паритетом auto (X1 lidar) + manual (всі моделі) + full dual-extruder (H2D).

Background research: `temp/printer_settings/flow_pa_calibration_research.md`.

---

## Architecture

Один уніфікований wizard, доступний через kebab-меню на картці принтера → **"Filament Calibration"** (НЕ плутати з існуючим "Calibration" kebab item — той ставиться для bitfield-cali: auto bed levelling / vibration / motor noise / nozzle offset / etc; залишається як є).

Шлях даних:

```
[User opens wizard]
  → POST /printers/{id}/calibration/sessions
  → CalibrationService.start_calibration
  → AUTO (X1+lidar): MQTT extrusion_cali mode=0
  → MANUAL: copy 3MF asset → tag PrintQueueItem.is_calibration → background_dispatch
  → live progress via WS (printer.{id} channel)
  → on completion:
       AUTO: parse push extrusion_cali_get_result → session.status=awaiting_user_input
       MANUAL: mc_print_stage flip → session.status=awaiting_user_input
  → User submits result via POST /sessions/{id}/manual-result OR /auto-result
  → CalibrationService.save_result
  → INSERT filament_calibration (is_active=True, попередні false)
  → MQTT extrusion_cali_set → printer-side history (16-slot)
  → session.status=saved
  → На наступному dispatch'і з цим filament_id:
       MQTT extrusion_cali_sel(ams_id, tray_id, cali_idx) перед print start
```

---

## Data Model

Нова таблиця `filament_calibration` (m062). Per-filament-type, per-printer-model. Many rows per combo (history), один `is_active=True` на combo.

```python
class FilamentCalibration(Base):
    __tablename__ = "filament_calibration"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Identity
    printer_model: Mapped[str] = mapped_column(String(50), nullable=False)  # "P1S", "X1C", "H2D Pro"
    filament_id: Mapped[str] = mapped_column(String(50), nullable=False)
    filament_setting_id: Mapped[str | None] = mapped_column(String(100))
    nozzle_diameter: Mapped[float] = mapped_column(Float, nullable=False)
    nozzle_volume_type: Mapped[str] = mapped_column(String(20), nullable=False)  # standard|high_flow|tpu_high_flow|hybrid
    extruder_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Result payload
    pa_k_value: Mapped[float | None] = mapped_column(Float)
    pa_n_coef: Mapped[float | None] = mapped_column(Float)
    flow_ratio: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[int | None] = mapped_column(Integer)  # 0=success, 1=uncertain, 2=failed

    # Provenance
    cali_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)  # auto|manual
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    cali_idx: Mapped[int | None] = mapped_column(Integer)  # printer-side history slot

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    calibrated_on_printer_id: Mapped[int | None] = mapped_column(ForeignKey("printers.id", ondelete="SET NULL"))
    calibrated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_filament_cali_lookup",
              "printer_model", "filament_id", "nozzle_diameter", "nozzle_volume_type", "extruder_id"),
        # Partial unique: один is_active=True на combo
        Index("ux_filament_cali_active",
              "printer_model", "filament_id", "nozzle_diameter", "nozzle_volume_type", "extruder_id",
              unique=True,
              postgresql_where=text("is_active = true"),
              sqlite_where=text("is_active = 1")),
    )
```

`CalibrationSession` (m062 same migration) — orchestration row, не persistent storage of cali values.

```python
class CalibrationSession(Base):
    __tablename__ = "calibration_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    cali_mode: Mapped[str]                    # pa_line | pa_pattern | pa_tower | auto_pa_line | flow_rate | temp_tower | vol_speed_tower | vfa_tower | retraction_tower
    method: Mapped[str]                        # auto | manual
    nozzle_diameter: Mapped[float]
    nozzle_volume_type: Mapped[str]
    extruder_id: Mapped[int]
    filaments_json: Mapped[str]                # snapshot of input
    status: Mapped[str]                         # running | awaiting_user_input | saved | cancelled | failed
    mqtt_sequence_id: Mapped[str | None]
    print_queue_item_id: Mapped[int | None] = mapped_column(ForeignKey("print_queue_items.id"))

    # Flow Rate 2-stage chain
    parent_session_id: Mapped[int | None] = mapped_column(ForeignKey("calibration_session.id"))
    stage: Mapped[int] = mapped_column(Integer, default=1)  # 1=coarse, 2=fine
    coarse_ratio: Mapped[float | None]

    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
```

`CalibrationAudit` (m062 same migration) — мирорить існуючі pattern (ams_setting_audit / printer_setting_audit):

```python
class CalibrationAudit(Base):
    __tablename__ = "calibration_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"))
    session_id: Mapped[int | None] = mapped_column(ForeignKey("calibration_session.id", ondelete="SET NULL"))
    filament_calibration_id: Mapped[int | None] = mapped_column(ForeignKey("filament_calibration.id", ondelete="SET NULL"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str]                        # start_session | save_result | sync_printer | delete | set_active | cancel
    payload_json: Mapped[str]
    sequence_id: Mapped[str | None]
    result: Mapped[str]                         # ok | error
    error_message: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
```

**Existing `spool_k_profile`** — НЕ deprecate. Поточна роль: per-spool sync з printer KProfile (per-printer view). Нова `filament_calibration` — це per-filament-type конфіг, інша абстракція. Поки що — два механізми паралельно. Dispatch lookup пріоритет: spool's `spool_k_profile` (якщо є) → `filament_calibration` active row (fallback). У фазі-1 dispatch чіпає тільки `filament_calibration` через `extrusion_cali_sel`.

**`PrintArchive.is_calibration: bool`** (m062 add_column, default False) — мітка що друк був калібровкою. `PrintArchive.calibration_session_id: int | None` FK. Без спеціальних UI-фільтрів у phase-1 — просто прапор. UI-обробку (окрема вкладка / filter chip) залишаємо на потім.

**`PrintQueueItem.is_calibration: bool`** (m062 add_column) + `calibration_session_id: int | None` FK — щоб dispatcher знав tagging-route.

---

## MQTT Layer (`backend/app/services/bambu_mqtt.py`)

### Існуючі (вирівнюємо payload до BS):

```python
def extrusion_cali_set(
    self,
    *,
    nozzle_diameter: float,
    filaments: list[dict],   # повний BS payload (k_value, n_coef, ams_id, slot_id, filament_id, setting_id, name, ...)
) -> tuple[bool, str | None]: ...

def extrusion_cali_sel(
    self,
    *,
    ams_id: int,
    tray_id: int,
    cali_idx: int,
    extruder_id: int = 0,
    nozzle_diameter: float | None = None,
) -> tuple[bool, str | None]: ...

def extrusion_cali_del(
    self,
    *,
    extruder_id: int,
    nozzle_id: str,
    filament_id: str,
    cali_idx: int,
    nozzle_diameter: float,
) -> tuple[bool, str | None]: ...
```

### Нові publishers:

```python
def extrusion_cali_start(
    self,
    *,
    nozzle_diameter: float,
    cali_mode: int,                # 0=auto (X1), 1=manual
    filaments: list[dict],
) -> tuple[bool, str | None]:
    """MQTT print.command='extrusion_cali'."""

def flow_rate_cali_start(
    self,
    *,
    nozzle_diameter: float,
    filaments: list[dict],         # з flow_rate populated
) -> tuple[bool, str | None]:
    """MQTT print.command='extrusion_cali' з flow_rate field для X1 auto-flow."""

def extrusion_cali_query_history(
    self,
    *,
    nozzle_diameter: float,
    extruder_id: int = 0,
) -> tuple[bool, str | None]:
    """MQTT print.command='extrusion_cali_get'. Просить поточну history з принтера."""

def extrusion_cali_query_result(
    self,
    *,
    nozzle_diameter: float,
) -> tuple[bool, str | None]:
    """MQTT print.command='extrusion_cali_get_result'. Запит auto-cali результату."""
```

### Parser extensions

Нові dataclass'и + поля у `PrinterState`:

```python
@dataclass
class ExtrusionCaliResult:
    tray_id: int
    ams_id: int
    slot_id: int
    extruder_id: int
    nozzle_diameter: float
    nozzle_volume_type: str
    filament_id: str
    setting_id: str
    k_value: float
    n_coef: float
    confidence: int
    nozzle_pos_id: int = -1
    nozzle_sn: str = ""


@dataclass
class PACalibHistoryEntry:
    cali_idx: int
    name: str
    filament_id: str
    setting_id: str
    nozzle_diameter: float
    nozzle_volume_type: str
    extruder_id: int
    k_value: float
    n_coef: float


@dataclass
class PrinterState:
    # ... існуючі ...
    extrusion_cali_results: list[ExtrusionCaliResult] = field(default_factory=list)
    extrusion_cali_session_id: str | None = None
    extrusion_cali_status: str = "idle"   # idle | running | completed | failed
    extrusion_cali_history: list[PACalibHistoryEntry] = field(default_factory=list)
    is_support_pa_calibration: bool = False
    is_support_auto_flow_calibration: bool = False
```

`_on_message` обробляє:
- `print.command == "extrusion_cali_get_result"` → парс list → `state.extrusion_cali_results`; status=`completed`
- `print.command == "extrusion_cali_get"` → парс history → `state.extrusion_cali_history`
- `mc_print_stage` flip IDLE + activated `extrusion_cali_session_id` + `gcode_file contains "auto_cali"` → status=`completed`
- HMS error під час cali → status=`failed`
- `print.support_auto_flow_calibration` / capability flags → відповідні bool

---

## Service Layer (`backend/app/services/calibration_service.py`)

```python
class CalibrationService:
    async def start_calibration(
        self,
        printer_id: int,
        cali_mode: CalibMode,
        method: CaliMethod,
        filaments: list[CalibFilamentInput],
        nozzle_diameter: float,
        nozzle_volume_type: str,
        extruder_id: int,
        user_id: int,
        db: AsyncSession,
    ) -> CalibrationSession:
        """
        Branch by (cali_mode, method):

        AUTO (cali_mode in {auto_pa_line, flow_rate} AND method=auto AND printer supports lidar):
          → Validate via compute_calibration_supports
          → Build extrusion_cali / flow_rate_cali payload (filaments[] з ams_id/slot_id/nozzle_id)
          → client.extrusion_cali_start(cali_mode=0)  OR  client.flow_rate_cali_start(...)
          → state.extrusion_cali_session_id = MQTT sequence_id
          → INSERT CalibrationSession status=running

        MANUAL (всі інші):
          → Resolve asset path: backend/app/data/calib_assets/{pressure_advance|filament_flow|temp_tower|...}/<file>.3mf
          → Copy to /tmp/cali/{uuid}.3mf
          → Build PrintQueueItem з is_calibration=True, calibration_session_id=session.id
          → background_dispatch.enqueue
          → on_print_start hook → captures MQTT sequence_id → links to session
          → on_print_complete → session.status=awaiting_user_input → WS notify

          Towers (temp_tower / vol_speed_tower / vfa_tower / retraction_tower):
            те саме що manual, але на completion status=saved одразу (без user input);
            wizard закривається з finish-message "Print complete — read result manually in your slicer".
            Жодних filament_calibration rows не пишеться.
        """

    async def submit_manual_result(
        self,
        session_id: int,
        *,
        best_line_index: int | None = None,
        coarse_modifier: int | None = None,
        skip_fine: bool = False,
        fine_modifier: int | None = None,
        db: AsyncSession,
    ) -> ManualResultOut:
        """
        PA Line/Pattern/Tower:
          K = start_pa + best_line_index * step_pa   # range з cali_mode metadata (зашитий per-mode dict)
          → save_result(pa_k_value=K, ...)

        Flow Rate coarse (stage=1):
          coarse_ratio = (100 + coarse_modifier) / 100   # base=1.0
          IF skip_fine: → save_result(flow_ratio=coarse_ratio, ...)
          ELSE: → нова session stage=2, parent_session_id=current,
                  coarse_ratio collected; повертаємо новий session_id для UI

        Flow Rate fine (stage=2):
          fine_ratio = parent.coarse_ratio * (100 + fine_modifier) / 100
          → save_result(flow_ratio=fine_ratio, ...)
        """

    async def submit_auto_result(
        self,
        session_id: int,
        edits: list[AutoResultEdit],     # per-row: pick / skip + name override + K override
        db: AsyncSession,
    ) -> list[FilamentCalibration]:
        """
        Read state.extrusion_cali_results for session's printer.
        Apply edits, call save_result for each picked row.
        """

    async def save_result(
        self,
        session: CalibrationSession,
        payload: ResultPayload,
        db: AsyncSession,
    ) -> FilamentCalibration:
        """
        1. UPDATE filament_calibration SET is_active=False WHERE combo matches
        2. INSERT new row, is_active=True
        3. MQTT client.extrusion_cali_set(filaments=[{...}])
        4. Wait push extrusion_cali_get з оновленою history (timeout 5s, non-blocking)
           → update row.cali_idx якщо знаходимо match
        5. session.status=saved
        6. CalibrationAudit row (action=save_result)
        """

    async def cancel_session(self, session_id: int, db: AsyncSession) -> None:
        """
        IF status=running AND method=auto: MQTT print.command='stop'
        IF status=running AND method=manual AND print active: те саме (cancel print)
        IF status=awaiting_user_input: просто mark cancelled
        Write CalibrationAudit (action=cancel).
        """
```

### Apply path — гібрид (auto-bind on save + per-dispatch re-sel + manual set-active sel)

**Контекст BS-поведінки** (для порівняння): у BS `extrusion_cali_sel` викликається **тільки** з `AMSMaterialsSetting.cpp` — юзер вручну відкриває AMS slot dialog, обирає PA profile з dropdown, шле sel. НЕ викликається з save dialog wizard'а, НЕ з print dispatch. Принтер прошивка має внутрішню логіку auto-pick за `filament_id` коли біндинг `cali_idx = -1` (default item у dropdown).

BamDude вибирає **гібрид**: zero-touch UX + sync на кожен dispatch. Три точки виклику `extrusion_cali_sel`:

**1) Auto-bind на Save.** Після `save_result` записує row у `filament_calibration` (`is_active=True`) і дочекався `cali_idx` з push `extrusion_cali_get` → одразу:

```python
# у save_result, після cali_idx resolved:
client.extrusion_cali_sel(
    ams_id=session.ams_id,
    tray_id=session.tray_id,
    cali_idx=new_row.cali_idx,
    extruder_id=session.extruder_id,
    nozzle_diameter=session.nozzle_diameter,
)
```

Юзер каже "save" — слот на якому калібрував одразу прив'язаний. Друк через будь-яке джерело (BS / BamDude / екран принтера) бере новий профіль.

**2) Per-dispatch re-sel.** На звичайний (НЕ-cali) print dispatch — додатковий sync. Покриває кейс: юзер у `CalibrationHistoryModal` руками перемкнув active row — наступний дисптч з BamDude підхопить.

```python
# у background_dispatch._run_one_dispatch перед print_start (новий-pathway):
for filament_slot in slots_used:
    cali = await _resolve_active_calibration(
        printer_model=printer.model,
        filament_id=filament_slot.filament_id,
        nozzle_dia=nozzle.diameter,
        nozzle_vol_type=nozzle.volume_type,
        extruder_id=filament_slot.extruder_id,
    )
    if cali and cali.cali_idx is not None:
        client.extrusion_cali_sel(
            ams_id=filament_slot.ams_id,
            tray_id=filament_slot.tray_id,
            cali_idx=cali.cali_idx,
            extruder_id=cali.extruder_id,
            nozzle_diameter=cali.nozzle_diameter,
        )
```

Якщо `_resolve_active_calibration` нічого не повертає — пропускаємо sel; прошивка сама вирішує per `filament_id` (firmware-auto-pick).

Якщо `cali.cali_idx is None` (не sync'нулося до printer history з якоїсь причини) — пропускаємо + warning у `calibration_audit`. UI у `HistoryModal` показує сигнал "Not synced" з ретрай-кнопкою.

**3) Manual set-active у HistoryModal.** Endpoint `POST /filament-calibrations/{id}/set-active` робить три речі:

```python
# flip is_active у filament_calibration: combo siblings false, цей true
# AND для кожного AMS slot який зараз заряджений цим filament_id:
client.extrusion_cali_sel(ams_id=..., tray_id=..., cali_idx=row.cali_idx, ...)
```

Тобто set-active не лише оновлює БД — а й одразу пушить на принтер для всіх відповідних слотів. Користувач натиснув "Set Active" → next print відразу з новим профілем.

### `_resolve_active_calibration` matching

```python
async def _resolve_active_calibration(
    db: AsyncSession,
    *,
    printer_model: str,
    filament_id: str,
    nozzle_dia: float,
    nozzle_vol_type: str,
    extruder_id: int,
) -> FilamentCalibration | None:
    """
    SELECT FROM filament_calibration
    WHERE printer_model = :pm
      AND filament_id = :fid
      AND nozzle_diameter = :nd
      AND nozzle_volume_type = :nvt
      AND extruder_id = :eid
      AND is_active = True
    LIMIT 1;
    """
```

Без match'у → return None → dispatch не шле sel → firmware fallback (`cali_idx=-1` semantics).

---

## Capability Helper (`services/printer_capabilities.py`)

Розширюємо існуючий файл новою функцією:

```python
def compute_calibration_supports(
    state: PrinterState,
    printer_model: str,
    module_vers: dict,
) -> dict:
    return {
        # Manual завжди дозволено
        "pa_manual": True,
        "flow_manual": True,
        "temp_tower": True,
        "vol_speed_tower": True,
        "vfa_tower": True,
        "retraction_tower": True,
        # Auto — тільки моделі з лідаром + capability flag з push
        "pa_auto": _has_lidar(printer_model) and state.is_support_pa_calibration,
        "flow_auto": _has_lidar(printer_model) and state.is_support_auto_flow_calibration,
        # Dual-extruder
        "dual_extruder": printer_model in {"H2D", "H2D Pro"},
        "extruders": _list_extruders(printer_model),
        # Nozzle info з PrinterState.nozzles
        "nozzles": [
            {
                "id": i,
                "diameter": n.diameter,
                "type": n.type,
                "flow_type": n.flow_type,
            }
            for i, n in enumerate(state.nozzles or [])
        ],
    }


_LIDAR_MODELS = frozenset({"X1", "X1C", "X1E", "H2D", "H2D Pro"})

def _has_lidar(model: str) -> bool:
    return model in _LIDAR_MODELS

def _list_extruders(model: str) -> list[dict]:
    if model in {"H2D", "H2D Pro"}:
        return [{"id": 0, "name": "Right"}, {"id": 1, "name": "Left"}]
    return [{"id": 0, "name": "Main"}]
```

---

## REST API

Новий router `backend/app/api/routes/filament_calibration.py`, prefix `/printers` + `/calibration` (mixed; registered у `main.py`). Permission `PRINTERS_UPDATE`.

| Method + Path | Body / Query | Response |
|---|---|---|
| `GET /printers/{id}/calibration/capabilities` | — | `CalibCapabilities` |
| `POST /printers/{id}/calibration/sessions` | `StartSessionIn` | `CalibrationSessionOut` |
| `GET /calibration/sessions/{id}` | — | `CalibrationSessionOut` + auto results if available |
| `POST /calibration/sessions/{id}/manual-result` | `ManualResultIn` | next session OR list of saved `FilamentCalibrationOut` |
| `POST /calibration/sessions/{id}/auto-result` | `AutoResultIn` | list of saved `FilamentCalibrationOut` |
| `POST /calibration/sessions/{id}/cancel` | — | 204 |
| `GET /calibration/sessions?printer_id=…&status=awaiting_user_input` | filters | `list[CalibrationSessionOut]` (для resume banner) |
| `GET /filament-calibrations` | query filters | `list[FilamentCalibrationOut]` |
| `GET /filament-calibrations/{id}` | — | `FilamentCalibrationOut` |
| `POST /filament-calibrations/{id}/set-active` | — | updated row + попередні false'нуті + MQTT `extrusion_cali_sel` для всіх AMS slots з відповідним `filament_id` |
| `DELETE /filament-calibrations/{id}` | — | 204 (+ MQTT `extrusion_cali_del` if cali_idx) |
| `GET /printers/{id}/calibration/history` | `?nozzle_diameter&extruder_id` | `list[PACalibHistoryEntryOut]` (з PrinterState) |
| `POST /printers/{id}/calibration/history/refresh` | — | 202 (subscribe WS для готовності) |

### Concurrent session guard

`POST /sessions` для принтера де вже є active session (status in {running, awaiting_user_input}) → **409 conflict** з тілом `{detail: "active_session_exists", session_id: <id>}`. UI пропонує "Resume / Cancel + new".

### WebSocket events (existing `printer.{id}` channel)

- `calibration.started` — `{session_id, mode, method}`
- `calibration.progress` — re-use existing print progress fields
- `calibration.completed` — `{session_id, awaiting_input: bool}`
- `calibration.failed` — `{session_id, error}`

---

## Frontend

### Wizard shell: `frontend/src/components/FilamentCalibrationModal.tsx`

State-machine driven через `useFilamentCalibration` hook. 9 sub-pages:

1. `CalibrationStartPage` — mode picker (radio cards grouped: PA / Flow Rate / Towers). Auto-rows dimmed з tooltip якщо `!capabilities.pa_auto`.
2. `CalibrationPresetPage` — per-extruder tabs (H2D), AMS slot picker, temps inputs, max vol speed input. Validation: ≥1 filament selected per active extruder.
3. `CalibrationRunningPage` — re-use existing live-progress block (camera snapshot + stage + progress + ETA). `[Cancel calibration]`.
4. `CalibrationManualSavePage` (PA Line/Pattern/Tower) — combo "Line index" → live K calc → name + notes inputs + Sync toggle.
5. `CalibrationCoarseSavePage` (Flow Rate stage 1) — combo `[-20, -15, -10, -5, 0, 5, 10, 15, 20]` → live coarse ratio → "Skip fine" checkbox.
6. `CalibrationFineSavePage` (Flow Rate stage 2) — combo `[-5, -2, 0, +2, +5, +10, +15]` → live fine ratio.
7. `CalibrationAutoSavePage` (X1 auto) — multi-row list of `ExtrusionCaliResult`, per-row "Apply" checkbox + name input + K/N override.
8. `CalibrationTowerFinishPage` (Temp/VolSpeed/VFA/Retraction) — "Print complete. Read result manually in your slicer."
9. `CalibrationFinishPage` (PA + Flow Rate save success) — "PLA Basic — PA 0.048 is active. Next print uses it." + [View History] [Calibrate Another] [Close].

### `CalibrationHistoryModal.tsx`

Accessible з [📜] кнопки у wizard finish + з kebab "Filament Calibration History".

- Per-printer view
- Grouped by nozzle diameter
- Per-row: filament name | mode | K/flow_ratio | source | created_at | Active marker | dropdown actions (Set Active, Re-calibrate, Delete)
- `[Refresh from printer]` → POST `/calibration/history/refresh` → subscribe WS

### Kebab menu (PrintersPage)

```tsx
<KebabMenuItem
  icon="droplet"
  label={t("filamentCali.title")}
  onClick={() => openFilamentCalibrationModal(printer.id)}
  disabled={!user.hasPermission("printers:update") || !printer.online}
/>
<KebabMenuItem
  icon="history"
  label={t("filamentCali.history")}
  onClick={() => openCalibrationHistoryModal(printer.id)}
  disabled={!user.hasPermission("printers:read")}
/>
```

### Resume banner

При відкритті `FilamentCalibrationModal` — GET `/calibration/sessions?status=awaiting_user_input&user_id=current` для цього printer:

```
┌─ ⚠ Незавершена калібровка ──────────────────────────────┐
│ PA Line · PLA Basic · 2026-05-12 14:23                   │
│                                  [Resume] [Discard] [×]   │
└──────────────────────────────────────────────────────────┘
```

Resume → wizard переходить одразу на крок 4-6 (manual_save / coarse_save / fine_save) з підвантаженим state.

### Hook: `useFilamentCalibration.ts`

TanStack Query orchestrator:
- `useCapabilities(printerId)`
- `useStartSession()` mutation
- `useSubmitManualResult()`
- `useSubmitAutoResult()`
- `useCancelSession()`
- `useFilamentCalibrationList()`
- `useCalibrationHistory(printerId)`
- `useResumeSessions(printerId)`
- WS subscriber для `calibration.*` events → auto-advance wizard step

### i18n

Новий namespace `filamentCali.*` у `en.ts` + `uk.ts` — ~80 keys (mode labels, page headers, validation, status, history actions). Обов'язково обидва locales.

### Styles

`bg-bambu-dark*` CSS-vars (як AMS / Printer Settings). Modal width 720px (більше для 9-step wizard).

---

## Calibration Assets

Shipping BS resources як-є під AGPL-3.0 compat.

```
backend/app/data/calib_assets/
├── pressure_advance/
│   ├── pa_line_0.4.3mf      # range 0.0–0.1 step 0.002 (50 lines), single per nozzle dia
│   ├── pa_line_0.2.3mf
│   ├── pa_line_0.6.3mf
│   ├── pa_line_0.8.3mf
│   ├── pa_pattern_0.4.3mf   # grid pattern
│   ├── pa_pattern_0.2.3mf
│   ├── pa_pattern_0.6.3mf
│   ├── pa_pattern_0.8.3mf
│   └── pa_tower_0.4.3mf     # PA tower
├── filament_flow/
│   ├── flowrate_pass1_0.4.3mf  # coarse −20..+20 (9 blocks)
│   ├── flowrate_pass1_0.2.3mf
│   ├── flowrate_pass1_0.6.3mf
│   ├── flowrate_pass1_0.8.3mf
│   ├── flowrate_pass2_0.4.3mf  # fine refined
│   ├── flowrate_pass2_0.2.3mf
│   ├── flowrate_pass2_0.6.3mf
│   └── flowrate_pass2_0.8.3mf
├── temp_tower/
│   └── temp_tower_0.4.3mf
├── volumetric_speed/
│   └── vol_speed_tower_0.4.3mf
├── vfa/
│   └── vfa_tower_0.4.3mf
├── retraction/
│   └── retraction_tower_0.4.3mf
└── README.md   # source attribution → BambuStudio/resources/calib/, AGPL-3.0
```

Diameter-specific варіанти: для 0.2/0.4/0.6/0.8. Якщо BS поставляє лише 0.4 — для інших диаметрів flow_rate/temp/vfa/retraction-towers — fallback на 0.4 з UI warning "Asset is 0.4-only; result may be inaccurate for your nozzle".

PA Line range metadata зашита у service (`PA_LINE_RANGE = (0.0, 0.1, 0.002, 50)`), не у 3MF. Не дозволяємо custom range у phase-1.

---

## Testing

### Backend

`backend/tests/unit/services/`
- `test_calibration_service.py` — start/submit/save/cancel; PA K calc; Flow ratio calc; auto edits; stage-2 chain
- `test_bambu_mqtt_calibration.py` — payload-перевірки на всі нові publishers + parser-перевірки на extrusion_cali_get_result / extrusion_cali_get / mc_print_stage transition
- `test_printer_capabilities_cali.py` — matrix: X1C / X1E / P1S / P1P / A1 / A1 mini / H2D / H2D Pro / unknown
- `test_calibration_dispatch_apply.py` — `_resolve_active_calibration` + `extrusion_cali_sel` triggered перед print start

`backend/tests/integration/`
- `test_m062_filament_calibration_migration.py` — DDL works SQLite + Postgres; partial unique index
- `test_calibration_routes.py` — start session (auto + manual), submit manual (PA + Flow coarse + Flow fine chain), submit auto, set-active, delete, cancel, capabilities, concurrent guard 409, permission 403

### Frontend

`frontend/src/__tests__/components/`
- `FilamentCalibrationModal.test.tsx` — renders Start, mode pick advances, disabled modes show tooltip, H2D shows extruder tabs
- `CalibrationManualSavePage.test.tsx` — K calc updates with line idx
- `CalibrationCoarseSavePage.test.tsx` — Skip Fine → save flow vs Continue → stage 2
- `CalibrationHistoryModal.test.tsx` — Set Active sends correct request, Active row marked

`frontend/src/__tests__/hooks/`
- `useFilamentCalibration.test.tsx` — WS auto-advance step on `calibration.completed`

---

## Error & Edge-case Handling

| Edge case | Behavior |
|---|---|
| Printer disconnects mid-wizard | Modal disables actions + toast "Printer offline"; session persists (status="awaiting_user_input" чи "running"); WS reconnect → wizard resumes |
| Auto path: `extrusion_cali_get_result` не приходить за 60 s після `mc_print_stage==1` | session.status=`failed`, error="No lidar result"; UI shows "Retry as manual" button |
| Manual print fails (HMS) | session.status=`failed`; wizard pivots to "Print failed — see [archive entry]" з link |
| User закриває tab під час awaiting_user_input | session persists; resume banner при наступному відкритті wizard'a |
| MQTT `extrusion_cali_set` publish fail | row saved з `cali_idx=NULL`; UI warning "Saved locally, not synced — [Retry sync]" button (POST `/filament-calibrations/{id}/sync-to-printer`) |
| User видаляє spool з active row binding | Row залишається (`filament_id` independent of spool_id); just orphans the spool linkage |
| Filament_id невідомий (custom material) | Дозволяємо cali; row пише `filament_id="custom"` + user-entered `name` |
| Concurrent cali на тому самому принтері | 409 з посиланням на existing session |
| Spool not loaded into AMS під час preset page | Validation error — "Select a loaded spool"; external spool selector dimmed якщо `support_virtual_tray=False` |
| H2D — одна extruder calibrating, інша idle | OK; per-extruder session; UI tab показує per-extruder status. Manual: один за одним (concurrent guard); Auto: можна разом через `filaments[]` |

### Explicit deferrals (phase-2+)

1. **External spool** (virtual tray, `tray_id >= 0x10000`) — phase-1 дозволяємо manual, blockуємо auto.
2. **PA range customization** (user start/end/step) — фіксований BS-default у phase-1.
3. **Spoolman sync** — `filament_calibration` → Spoolman не sync'имо. `spoolman_k_profile` залишається як було.
4. **History edit** (re-edit K value у HistoryModal) — phase-1 тільки view + delete + set-active.
5. **Per-printer override** (один фізичний P1S vs всі P1S) — phase-1 per-`printer_model`; додавання `printer_id_override` коли реально потрібно.
6. **Auto-application для друків з зовнішнього джерела** (BS, екран принтера) — працює: auto-bind на Save і set-active викликають `extrusion_cali_sel`, який принтер пам'ятає per AMS slot. Тобто будь-який наступний друк через цей слот (через будь-яке джерело) бере прив'язаний профіль. Не покривається тільки edge case: юзер вручну перемкнув active row у BamDude UI **і** одразу запустив друк не через BamDude — тоді sel вже надіслано у set-active, тож все одно покривається.

---

## Documentation Updates

Після impl:
- `CHANGELOG.md` — under `[Unreleased]` (BamDude-side): один рядок про Filament Calibration wizard.
- `docs.bamdude.top/docs/features/printer-control.uk.md` + `.md` — новий `## Filament Calibration Wizard` розділ (за pattern Printer Settings Dialog).
- `bamdude.top/src/content/{en,uk}/features-grouped.json` — додати пункт у Monitoring & Control group.

---

## Implementation Phasing (для writing-plans)

Recommended chunks (це не для spec, це для plan-writer):

- **A.** m062 migration + 4 моделі + capabilities helper
- **B.** MQTT publishers + parser extensions + PrinterState fields
- **C.** CalibrationService (start/submit/save/cancel/apply-at-dispatch)
- **D.** REST endpoints + audit + concurrent guard
- **E.** 3MF assets ship + service asset resolver
- **F.** Frontend wizard shell + Start + Preset + Running pages
- **G.** Manual save pages (PA + Flow coarse + Flow fine)
- **H.** Auto save page + Tower finish page
- **I.** History modal + Resume banner
- **J.** Dual-extruder UI + per-extruder sessions
- **K.** i18n (en + uk) + tests + docs

Жорсткої залежності між F-J немає всередині — UI можна паралелити після C/D/E.
