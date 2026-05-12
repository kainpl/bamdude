# Printer Settings Dialog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port Bambu Studio's *Print Options* and *Printer Parts* dialogs into BamDude as a single tabbed modal opened from the printer-card kebab menu (3-dots). Existing `CalibrationModal` stays separate per the user's decision.

**Architecture:** Thin pass-through to MQTT, identical pattern to AMS Settings dialog. Printer is source of truth; we mirror state from `print.*` + `xcam.*` pushes into `PrinterState`, expose it over a new REST router gated by `printers:update`, and audit every applied write in a new `printer_setting_audit` table (m061). One frontend modal with 2 tabs (PrintOptions, PrinterParts) + new kebab menu item.

**Tech Stack:** Python 3.10 / FastAPI / SQLAlchemy 2.0 async / pytest · React 19 / TypeScript / Tailwind 4 / TanStack Query 5 / vitest.

**Spec:** `docs/superpowers/specs/2026-05-12-printer-settings-dialog-design.md`

---

## Pre-existing pieces (do NOT recreate)

These are already in BamDude and stay as-is:

- **CalibrationModal** — `frontend/src/components/CalibrationModal.tsx` + `bambu_mqtt.py::start_calibration(...)` (5 options, per-model gating). Lives under its own kebab menu item.
- **`set_xcam_option(module, enabled, print_halt, sensitivity)`** — `bambu_mqtt.py:3495`. We REUSE this from the new router for AI detector toggles.
- **`PrintOptions` dataclass** — `bambu_mqtt.py:115` with ~14 AI/sensor flags already parsed from `xcam.cfg` bitfield. We extend it with a few new bool fields rather than create a parallel structure.
- **`ams_setting_audit` + `m060`** — pattern template for our `printer_setting_audit` + `m061`.

---

## Open decisions resolved at plan-time

1. **Calibration scope:** Stays in its own `CalibrationModal`. Two kebab menu items (existing "Calibration" + new "Printer Settings"). NOT changing `start_calibration` backend signature.
2. **Migration number:** Next free is `m061` (current head: `m060_ams_setting_audit`).
3. **Parts editability:** Read-only on every model this iteration. The `set_nozzle` action exists in the discriminated union as a stub that returns `409 parts_not_editable`. Surface returns to phase-2 if real edit support is requested.
4. **Permission:** `Permission.PRINTERS_UPDATE` (same as AMS Settings).
5. **Stop-related options like SwitchBoard (open_door, purify_air):** 3-state ints — backend takes `int`, frontend renders segmented control.

---

## File Structure

**New files:**

- `backend/app/migrations/m061_printer_setting_audit.py` — audit table DDL.
- `backend/app/models/printer_setting_audit.py` — SQLAlchemy model + register.
- `backend/app/schemas/printer_settings.py` — Pydantic request/response (discriminated union body).
- `backend/app/api/routes/printer_settings.py` — GET + POST.
- `backend/app/services/printer_capabilities.py` — `compute_printer_supports(state, model, module_vers)`.
- `backend/tests/unit/services/test_bambu_mqtt_printer_settings.py` — publisher unit tests.
- `backend/tests/unit/services/test_printer_capabilities.py` — gating unit tests.
- `backend/tests/integration/test_m061_printer_setting_audit_migration.py` — migration smoke.
- `backend/tests/integration/test_printer_settings_routes.py` — API integration.
- `frontend/src/components/PrinterSettingsModal.tsx` — tabbed modal.
- `frontend/src/components/PrintOptionsTab.tsx` — tab 1.
- `frontend/src/components/PrinterPartsTab.tsx` — tab 2.
- `frontend/src/hooks/usePrinterSettings.ts` — TanStack Query + client hold-timer.
- `frontend/src/__tests__/components/PrinterSettingsModal.test.tsx` — vitest.

**Modified files:**

- `backend/app/services/bambu_mqtt.py` — add ~10 new publishers + 4 new fields on `PrintOptions` dataclass + new push-parser block for `print.print_option`/`air_purification`/etc. echoes + hold-timer dict on `PrinterState`.
- `backend/app/models/__init__.py` — register `PrinterSettingAudit`.
- `backend/app/main.py` — `include_router(printer_settings_routes.router)`.
- `frontend/src/api/client.ts` — 2 new methods + types.
- `frontend/src/pages/PrintersPage.tsx` — new kebab menu item + mount modal.
- `frontend/src/i18n/locales/en.ts` — `printerSettings` block.
- `frontend/src/i18n/locales/uk.ts` — `printerSettings` block.
- `CHANGELOG.md` — one-line entry under `[Unreleased]`.

---

## Test Strategy

- TDD where natural: each new publisher gets its payload-shape unit test before implementation.
- Migration: smoke + idempotency (mirror of `test_m060_*`).
- Routes: auth gates, supports-gated 409, audit row on success+error, MQTT-publish failure → 504.
- Capability helper: parameterized over model codes (X1C, P1S, A1 Mini, H2D, unknown).
- Frontend: skeleton, hidden unsupported rows, optimistic toggle, revert on error, segmented control state changes.

---

## Task 1: Migration `m061_printer_setting_audit`

**Files:**
- Create: `backend/app/migrations/m061_printer_setting_audit.py`
- Create: `backend/tests/integration/test_m061_printer_setting_audit_migration.py`

- [ ] **Step 1: Write the migration smoke test**

```python
# backend/tests/integration/test_m061_printer_setting_audit_migration.py
"""Smoke test for m061 — printer_setting_audit table + index, idempotent."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m061_printer_setting_audit


@pytest_asyncio.fixture
async def engine_with_prereqs():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE printers (id INTEGER PRIMARY KEY, name TEXT)"))
        await conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)"))
    try:
        yield engine
    finally:
        await engine.dispose()


async def _table_exists(conn, name: str) -> bool:
    r = await conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"),
        {"n": name},
    )
    return r.scalar() is not None


async def _index_exists(conn, name: str) -> bool:
    r = await conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='index' AND name=:n"),
        {"n": name},
    )
    return r.scalar() is not None


@pytest.mark.asyncio
async def test_m061_creates_table_and_index(engine_with_prereqs):
    async with engine_with_prereqs.begin() as conn:
        await m061_printer_setting_audit.upgrade(conn)
        assert await _table_exists(conn, "printer_setting_audit")
        assert await _index_exists(conn, "ix_printer_setting_audit_printer")


@pytest.mark.asyncio
async def test_m061_is_idempotent(engine_with_prereqs):
    async with engine_with_prereqs.begin() as conn:
        await m061_printer_setting_audit.upgrade(conn)
        await m061_printer_setting_audit.upgrade(conn)
        assert await _table_exists(conn, "printer_setting_audit")
```

- [ ] **Step 2: Run — confirm fail (module missing)**

Run: `pytest backend/tests/integration/test_m061_printer_setting_audit_migration.py -v`
Expected: ImportError on `m061_printer_setting_audit`.

- [ ] **Step 3: Write the migration**

```python
# backend/app/migrations/m061_printer_setting_audit.py
"""Audit trail for the Printer Settings dialog (Print Options + Parts tabs).

Why this exists:
    Same rationale as ams_setting_audit (m060): BS has no RBAC, BamDude
    gates the dialog behind PRINTERS_UPDATE and records every applied
    change so operators can answer "who flipped auto-recovery last
    Thursday?" without diffing MQTT logs.

What this migration does:
    Creates printer_setting_audit with (printer_id, user_id, tab, action,
    payload_json, sequence_id, result, error_message, created_at) and a
    descending index on (printer_id, created_at DESC).

Idempotent: CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import table_exists

version = 61
name = "printer_setting_audit"


async def upgrade(conn):
    if not await table_exists(conn, "printer_setting_audit"):
        if is_postgres():
            await conn.execute(
                text(
                    """
                    CREATE TABLE printer_setting_audit (
                        id SERIAL PRIMARY KEY,
                        printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        tab VARCHAR(30) NOT NULL,
                        action VARCHAR(50) NOT NULL,
                        payload_json TEXT NOT NULL,
                        sequence_id VARCHAR(50),
                        result VARCHAR(20) NOT NULL,
                        error_message TEXT,
                        created_at TIMESTAMP NOT NULL DEFAULT now()
                    )
                    """
                )
            )
        else:
            await conn.execute(
                text(
                    """
                    CREATE TABLE printer_setting_audit (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        tab TEXT NOT NULL,
                        action TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        sequence_id TEXT,
                        result TEXT NOT NULL,
                        error_message TEXT,
                        created_at TIMESTAMP NOT NULL DEFAULT (CURRENT_TIMESTAMP)
                    )
                    """
                )
            )

    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_printer_setting_audit_printer "
            "ON printer_setting_audit(printer_id, created_at DESC)"
        )
    )
```

- [ ] **Step 4: Run tests — confirm pass**

Run: `pytest backend/tests/integration/test_m061_printer_setting_audit_migration.py -v`
Expected: 2 PASSED.

- [ ] **Step 5: Commit**

```bash
git add backend/app/migrations/m061_printer_setting_audit.py \
        backend/tests/integration/test_m061_printer_setting_audit_migration.py
git commit -m "feat(migrations): m061 printer_setting_audit for Printer Settings dialog"
```

---

## Task 2: `PrinterSettingAudit` model

**Files:**
- Create: `backend/app/models/printer_setting_audit.py`
- Modify: `backend/app/models/__init__.py`

- [ ] **Step 1: Write the model**

```python
# backend/app/models/printer_setting_audit.py
"""Audit row for one applied Printer Settings dialog change.

Written by backend/app/api/routes/printer_settings.py after each MQTT
publish (success → result='sent'; failure → result='error' +
error_message). Read by nobody yet — surfaced in a future viewer UI.

``tab`` discriminates which sub-dialog the change belongs to so a future
viewer can filter cheaply.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class PrinterSettingAudit(Base):
    __tablename__ = "printer_setting_audit"
    __table_args__ = (
        Index("ix_printer_setting_audit_printer", "printer_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(
        ForeignKey("printers.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    tab: Mapped[str] = mapped_column(String(30), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    sequence_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    result: Mapped[str] = mapped_column(String(20), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
```

- [ ] **Step 2: Register in models/__init__.py**

Open `backend/app/models/__init__.py`. After the existing `from backend.app.models.ams_setting_audit import AmsSettingAudit` line add:

```python
from backend.app.models.printer_setting_audit import PrinterSettingAudit
```

And add `"PrinterSettingAudit"` to the `__all__` list, alphabetically next to `"AmsSettingAudit"`.

- [ ] **Step 3: Verify import + metadata registration**

Run: `python -c "from backend.app.models.printer_setting_audit import PrinterSettingAudit; from backend.app.core.database import Base; print(PrinterSettingAudit.__tablename__, 'printer_setting_audit' in Base.metadata.tables)"`
Expected: `printer_setting_audit True`

- [ ] **Step 4: Commit**

```bash
git add backend/app/models/printer_setting_audit.py backend/app/models/__init__.py
git commit -m "feat(models): PrinterSettingAudit + register"
```

---

## Task 3: Extend `PrintOptions` + `PrinterState` for new fields

**Files:**
- Modify: `backend/app/services/bambu_mqtt.py` — `PrintOptions` dataclass (line ~115) + `PrinterState` (line ~137).

- [ ] **Step 1: Add new fields to `PrintOptions` dataclass**

Find `class PrintOptions:` (line ~115). After the existing `filament_tangle_detect: bool = False` field, append:

```python
    # New flags added for the Printer Settings dialog. Each is bool|None
    # (None = "printer hasn't reported"); the rest of the dataclass uses
    # plain bool defaults — we keep the original defaults intact and only
    # surface None for the new ones via the API.
    nozzle_blob_detect: bool | None = None
    sound_enable: bool | None = None
    save_remote_to_storage: int | None = None
    air_purification: int | None = None     # 0 Off / 1 Inside / 2 Outside
    open_door_check: int | None = None      # 0 Off / 1 Pause / 2 Halt
    plate_type_detect: bool | None = None   # build_plate_marker_detect echo
    plate_align_check: bool | None = None
    snapshot_enabled: bool | None = None
    fod_check: bool | None = None
    displacement_detection: bool | None = None
```

- [ ] **Step 2: Add hold-timer dict to `PrinterState`**

Find `ams_settings_hold: dict = field(default_factory=dict)` (added in the AMS Settings work, line ~225). After it, append:

```python
    # Hold-timer for Printer Settings dialog. Same 3 s TTL pattern as
    # ams_settings_hold — keys are flag names ("auto_recovery",
    # "sound_enable", "purify_air", "open_door", "spaghetti_detector", …)
    # mapped to epoch_seconds. Push parser ignores echoes for a key while
    # the hold is active.
    printer_settings_hold: dict = field(default_factory=dict)
```

- [ ] **Step 3: Smoke-check**

Run: `python -c "from backend.app.services.bambu_mqtt import PrintOptions, PrinterState; po = PrintOptions(); ps = PrinterState(); print(po.nozzle_blob_detect, po.air_purification, ps.printer_settings_hold)"`
Expected: `None None {}`

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/bambu_mqtt.py
git commit -m "feat(mqtt): PrintOptions + PrinterState fields for Printer Settings dialog"
```

---

## Task 4: New MQTT publishers (Print Options bool + int + snapshot)

**Files:**
- Modify: `backend/app/services/bambu_mqtt.py` — add publishers after `set_xcam_option` (line ~3495+).
- Create: `backend/tests/unit/services/test_bambu_mqtt_printer_settings.py`

- [ ] **Step 1: Write tests for the new publishers**

```python
# backend/tests/unit/services/test_bambu_mqtt_printer_settings.py
"""Unit tests for Printer Settings dialog publishers + hold-timer."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


@pytest.fixture
def mqtt_client():
    c = BambuMQTTClient(
        ip_address="192.168.1.100",
        serial_number="TESTPS001",
        access_code="12345678",
    )
    c._client = MagicMock()
    c.state.connected = True
    return c


def _payload(c) -> dict:
    call = c._client.publish.call_args
    _, payload, *_ = call.args
    return json.loads(payload)


# ---------- Bool toggles via print.command="print_option" ----------

@pytest.mark.parametrize(
    "method,field",
    [
        ("print_option_auto_recovery", "auto_recovery"),
        ("print_option_sound", "sound_enable"),
        ("print_option_filament_tangle", "filament_tangle_detect"),
        ("print_option_nozzle_blob", "nozzle_blob_detect"),
        ("print_option_plate_type", "build_plate_marker_detect"),
        ("print_option_plate_align", "plate_align_check"),
    ],
)
def test_print_option_bool_payload(mqtt_client, method, field):
    ok, seq = getattr(mqtt_client, method)(True)
    assert ok is True and seq is not None
    msg = _payload(mqtt_client)
    assert msg["print"]["command"] == "print_option"
    assert msg["print"][field] is True
    assert msg["print"]["sequence_id"] == seq


def test_print_option_bool_stamps_hold(mqtt_client):
    before = time.time()
    mqtt_client.print_option_auto_recovery(True)
    after = time.time()
    ts = mqtt_client.state.printer_settings_hold.get("auto_recovery")
    assert ts is not None and before <= ts <= after


def test_print_option_returns_false_when_disconnected(mqtt_client):
    mqtt_client.state.connected = False
    ok, seq = mqtt_client.print_option_auto_recovery(True)
    assert ok is False and seq is None


# ---------- Int toggles via print.command="print_option" ----------

@pytest.mark.parametrize(
    "method,field,value",
    [
        ("print_option_purify_air", "air_purification", 2),
        ("print_option_open_door", "xcam_door_open_check", 1),
        ("print_option_save_remote_to_storage", "xcam__save_remote_print_file_to_storage", 1),
    ],
)
def test_print_option_int_payload(mqtt_client, method, field, value):
    ok, seq = getattr(mqtt_client, method)(value)
    assert ok is True and seq is not None
    msg = _payload(mqtt_client)
    assert msg["print"]["command"] == "print_option"
    assert msg["print"][field] == value


# ---------- Camera snapshot ----------

def test_camera_snapshot_enable_payload(mqtt_client):
    ok, seq = mqtt_client.camera_snapshot_enable(True)
    assert ok is True and seq is not None
    msg = _payload(mqtt_client)
    assert msg["camera"]["command"] == "ipcam_cap_pic_set"
    assert msg["camera"]["control"] == "enable"


def test_camera_snapshot_disable_payload(mqtt_client):
    ok, _ = mqtt_client.camera_snapshot_enable(False)
    assert ok is True
    msg = _payload(mqtt_client)
    assert msg["camera"]["control"] == "disable"


def test_camera_snapshot_stamps_hold(mqtt_client):
    mqtt_client.camera_snapshot_enable(True)
    assert "snapshot" in mqtt_client.state.printer_settings_hold
```

- [ ] **Step 2: Run — confirm fails**

Run: `pytest backend/tests/unit/services/test_bambu_mqtt_printer_settings.py -v`
Expected: AttributeError for missing methods.

- [ ] **Step 3: Add publishers in bambu_mqtt.py**

Find `def set_xcam_option(` (line ~3495). After the closing of that method (its body ends with the publish call), append the new block:

```python
    # ---------- Printer Settings dialog publishers (Print Options tab) ----------
    # Each publisher matches BS DeviceCore/DevPrintOptions.cpp shapes. All use
    # ``print.command = "print_option"`` with one toggle field per call;
    # snapshot uses ``camera.command = "ipcam_cap_pic_set"``. Return
    # ``(success, sequence_id)``. Hold-timer is stamped on
    # ``state.printer_settings_hold`` so the push parser doesn't clobber
    # the optimistic value during the printer's confirm round-trip.

    def _publish_print_option_bool(self, field: str, hold_key: str, enabled: bool) -> tuple[bool, str | None]:
        if not self._client or not self.state.connected:
            return False, None
        self._sequence_id += 1
        seq = str(self._sequence_id)
        command = {"print": {"command": "print_option", "sequence_id": seq, field: bool(enabled)}}
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        self.state.printer_settings_hold[hold_key] = time.time()
        return True, seq

    def _publish_print_option_int(self, field: str, hold_key: str, value: int) -> tuple[bool, str | None]:
        if not self._client or not self.state.connected:
            return False, None
        self._sequence_id += 1
        seq = str(self._sequence_id)
        command = {"print": {"command": "print_option", "sequence_id": seq, field: int(value)}}
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        self.state.printer_settings_hold[hold_key] = time.time()
        return True, seq

    def print_option_auto_recovery(self, enabled: bool) -> tuple[bool, str | None]:
        return self._publish_print_option_bool("auto_recovery", "auto_recovery", enabled)

    def print_option_sound(self, enabled: bool) -> tuple[bool, str | None]:
        return self._publish_print_option_bool("sound_enable", "sound_enable", enabled)

    def print_option_filament_tangle(self, enabled: bool) -> tuple[bool, str | None]:
        return self._publish_print_option_bool("filament_tangle_detect", "filament_tangle", enabled)

    def print_option_nozzle_blob(self, enabled: bool) -> tuple[bool, str | None]:
        return self._publish_print_option_bool("nozzle_blob_detect", "nozzle_blob", enabled)

    def print_option_plate_type(self, enabled: bool) -> tuple[bool, str | None]:
        return self._publish_print_option_bool("build_plate_marker_detect", "plate_type", enabled)

    def print_option_plate_align(self, enabled: bool) -> tuple[bool, str | None]:
        return self._publish_print_option_bool("plate_align_check", "plate_align", enabled)

    def print_option_purify_air(self, value: int) -> tuple[bool, str | None]:
        return self._publish_print_option_int("air_purification", "purify_air", value)

    def print_option_open_door(self, value: int) -> tuple[bool, str | None]:
        return self._publish_print_option_int("xcam_door_open_check", "open_door", value)

    def print_option_save_remote_to_storage(self, value: int) -> tuple[bool, str | None]:
        return self._publish_print_option_int(
            "xcam__save_remote_print_file_to_storage", "save_remote_to_storage", value
        )

    def camera_snapshot_enable(self, enabled: bool) -> tuple[bool, str | None]:
        if not self._client or not self.state.connected:
            return False, None
        self._sequence_id += 1
        seq = str(self._sequence_id)
        command = {
            "camera": {
                "command": "ipcam_cap_pic_set",
                "sequence_id": seq,
                "control": "enable" if enabled else "disable",
            }
        }
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        self.state.printer_settings_hold["snapshot"] = time.time()
        return True, seq
```

- [ ] **Step 4: Run tests — confirm pass**

Run: `pytest backend/tests/unit/services/test_bambu_mqtt_printer_settings.py -v`
Expected: ~13 PASSED.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/bambu_mqtt.py \
        backend/tests/unit/services/test_bambu_mqtt_printer_settings.py
git commit -m "feat(mqtt): print_option publishers + camera_snapshot for Printer Settings dialog"
```

---

## Task 5: Wrap `set_xcam_option` for sensitivity-aware XCam writes

The existing `set_xcam_option` already covers the wire format; we add a thin convenience wrapper that stamps the hold-timer and matches the action-router's signature.

**Files:**
- Modify: `backend/app/services/bambu_mqtt.py` — add wrapper.
- Modify: `backend/tests/unit/services/test_bambu_mqtt_printer_settings.py` — append test.

- [ ] **Step 1: Append test**

```python
# Append to test_bambu_mqtt_printer_settings.py

def test_xcam_control_wrapper_returns_seq_and_stamps_hold(mqtt_client):
    ok, seq = mqtt_client.xcam_control_for_settings(
        "spaghetti_detector", enabled=True, sensitivity="high"
    )
    assert ok is True and seq is not None
    msg = _payload(mqtt_client)
    assert msg["xcam"]["command"] == "xcam_control_set"
    assert msg["xcam"]["module_name"] == "spaghetti_detector"
    assert msg["xcam"]["control"] is True
    assert msg["xcam"]["halt_print_sensitivity"] == "high"
    assert "spaghetti_detector" in mqtt_client.state.printer_settings_hold


def test_xcam_control_wrapper_sensitivity_optional(mqtt_client):
    ok, _ = mqtt_client.xcam_control_for_settings("fod_check", enabled=False, sensitivity=None)
    assert ok is True
    msg = _payload(mqtt_client)
    assert msg["xcam"]["module_name"] == "fod_check"
    assert msg["xcam"]["control"] is False
    # No sensitivity → field not sent
    assert "halt_print_sensitivity" not in msg["xcam"]
```

- [ ] **Step 2: Run — fail expected (AttributeError)**

Run: `pytest backend/tests/unit/services/test_bambu_mqtt_printer_settings.py -k xcam -v`
Expected: 2 FAIL.

- [ ] **Step 3: Add wrapper after `camera_snapshot_enable`**

```python
    def xcam_control_for_settings(
        self,
        module: str,
        enabled: bool,
        sensitivity: str | None = None,
    ) -> tuple[bool, str | None]:
        """Thin wrapper over ``set_xcam_option`` for the Printer Settings router.

        Unlike the existing ``set_xcam_option`` (which returns bool and
        always sends ``halt_print_sensitivity``), this:
          - returns (ok, sequence_id) for audit-trail correlation,
          - omits ``halt_print_sensitivity`` when ``sensitivity is None``,
          - stamps ``printer_settings_hold[module]``.
        Wire format from BS DevPrintOptions.cpp::command_xcam_control.
        """
        if not self._client or not self.state.connected:
            return False, None
        self._sequence_id += 1
        seq = str(self._sequence_id)
        command: dict = {
            "xcam": {
                "command": "xcam_control_set",
                "sequence_id": seq,
                "module_name": module,
                "control": bool(enabled),
                "enable": bool(enabled),
                "print_halt": True,
            }
        }
        if sensitivity is not None:
            command["xcam"]["halt_print_sensitivity"] = sensitivity
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        self.state.printer_settings_hold[module] = time.time()
        return True, seq
```

- [ ] **Step 4: Run — confirm pass**

Run: `pytest backend/tests/unit/services/test_bambu_mqtt_printer_settings.py -k xcam -v`
Expected: 2 PASSED.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/bambu_mqtt.py \
        backend/tests/unit/services/test_bambu_mqtt_printer_settings.py
git commit -m "feat(mqtt): xcam_control_for_settings wrapper with hold-timer"
```

---

## Task 6: Push-parser additions — `print.print_option` echoes

The printer echoes the toggle fields back in subsequent push messages after a `print_option` command. We mirror them into `PrintOptions` respecting the hold-timer. AI detector parsing (xcam.cfg bits) already exists for the legacy detectors; we only add the *direct field echoes* here.

**Files:**
- Modify: `backend/app/services/bambu_mqtt.py` — extend the print-data handler near the existing `auto_switch_filament` / `air_print_detect` echo block (added during AMS Settings work).
- Modify: `backend/tests/unit/services/test_bambu_mqtt_printer_settings.py` — append parser tests.

- [ ] **Step 1: Append parser tests**

```python
# Append to test_bambu_mqtt_printer_settings.py

class _FakeMQTTMsg:
    def __init__(self, payload_dict):
        import json as _json
        self.topic = ""
        self.payload = _json.dumps(payload_dict).encode("utf-8")


def test_parser_reads_print_option_bool_echoes(mqtt_client):
    msg = {
        "print": {
            "command": "push_status",
            "auto_recovery": True,
            "sound_enable": False,
            "filament_tangle_detect": True,
            "nozzle_blob_detect": True,
            "build_plate_marker_detect": False,
            "plate_align_check": True,
        }
    }
    mqtt_client._on_message(None, None, _FakeMQTTMsg(msg))
    po = mqtt_client.state.print_options
    assert po.auto_recovery_step_loss is True
    assert po.sound_enable is False
    assert po.filament_tangle_detect is True
    assert po.nozzle_blob_detect is True
    assert po.plate_type_detect is False
    assert po.plate_align_check is True


def test_parser_reads_print_option_int_echoes(mqtt_client):
    msg = {
        "print": {
            "command": "push_status",
            "air_purification": 2,
            "xcam_door_open_check": 1,
            "xcam__save_remote_print_file_to_storage": 0,
        }
    }
    mqtt_client._on_message(None, None, _FakeMQTTMsg(msg))
    po = mqtt_client.state.print_options
    assert po.air_purification == 2
    assert po.open_door_check == 1
    assert po.save_remote_to_storage == 0


def test_parser_respects_printer_settings_hold(mqtt_client):
    mqtt_client.state.print_options.auto_recovery_step_loss = False
    mqtt_client.state.printer_settings_hold["auto_recovery"] = time.time()
    msg = {"print": {"command": "push_status", "auto_recovery": True}}
    mqtt_client._on_message(None, None, _FakeMQTTMsg(msg))
    # Hold active → echo ignored, previous local value preserved.
    assert mqtt_client.state.print_options.auto_recovery_step_loss is False
```

- [ ] **Step 2: Run — confirm fail**

Run: `pytest backend/tests/unit/services/test_bambu_mqtt_printer_settings.py -k parser -v`
Expected: 3 FAIL (echoes not parsed).

- [ ] **Step 3: Locate existing print-data echo block and extend it**

Find the block added during AMS Settings work — it begins with the comment `# AMS Settings dialog echoes: the printer reflects ...` (search `auto_switch_filament` in `bambu_mqtt.py`). Right after that block, add:

```python
            # Printer Settings dialog echoes — direct field echoes for the
            # print_option toggles. Each respects a 3 s hold from
            # printer_settings_hold so a freshly-toggled flag isn't
            # immediately overwritten by the printer's confirm push.
            _ps_now = time.time()
            _ps_ttl = 3.0

            def _ps_hold(flag: str) -> bool:
                ts = self.state.printer_settings_hold.get(flag)
                return ts is not None and (_ps_now - ts) < _ps_ttl

            po = self.state.print_options
            if "auto_recovery" in print_data and not _ps_hold("auto_recovery"):
                po.auto_recovery_step_loss = bool(print_data["auto_recovery"])
            if "sound_enable" in print_data and not _ps_hold("sound_enable"):
                po.sound_enable = bool(print_data["sound_enable"])
            if "filament_tangle_detect" in print_data and not _ps_hold("filament_tangle"):
                po.filament_tangle_detect = bool(print_data["filament_tangle_detect"])
            if "nozzle_blob_detect" in print_data and not _ps_hold("nozzle_blob"):
                po.nozzle_blob_detect = bool(print_data["nozzle_blob_detect"])
            if "build_plate_marker_detect" in print_data and not _ps_hold("plate_type"):
                po.plate_type_detect = bool(print_data["build_plate_marker_detect"])
            if "plate_align_check" in print_data and not _ps_hold("plate_align"):
                po.plate_align_check = bool(print_data["plate_align_check"])
            if "air_purification" in print_data and not _ps_hold("purify_air"):
                po.air_purification = int(print_data["air_purification"])
            if "xcam_door_open_check" in print_data and not _ps_hold("open_door"):
                po.open_door_check = int(print_data["xcam_door_open_check"])
            if "xcam__save_remote_print_file_to_storage" in print_data and not _ps_hold("save_remote_to_storage"):
                po.save_remote_to_storage = int(print_data["xcam__save_remote_print_file_to_storage"])
```

- [ ] **Step 4: Run — confirm pass**

Run: `pytest backend/tests/unit/services/test_bambu_mqtt_printer_settings.py -k parser -v`
Expected: 3 PASSED.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/bambu_mqtt.py \
        backend/tests/unit/services/test_bambu_mqtt_printer_settings.py
git commit -m "feat(mqtt): parse print_option echoes for Printer Settings dialog"
```

---

## Task 7: `printer_capabilities` helper

Computes the `supports.*` map for the Print Options + Parts tabs. AI detector support, sensor support, dual-nozzle parts gating — all model-driven, no firmware-version gates this iteration.

**Files:**
- Create: `backend/app/services/printer_capabilities.py`
- Create: `backend/tests/unit/services/test_printer_capabilities.py`

- [ ] **Step 1: Write tests**

```python
# backend/tests/unit/services/test_printer_capabilities.py
"""Tests for compute_printer_supports — Print Options + Parts row visibility."""

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.printer_capabilities import compute_printer_supports


def _supports(model: str):
    return compute_printer_supports(PrinterState(), model, module_vers={})


def test_x1c_supports_ai_monitoring_and_door():
    s = _supports("X1C")
    # AI detectors (X1 has the full set)
    assert s["spaghetti_detector"] is True
    assert s["nozzleclumping_detector"] is True
    assert s["airprinting_detector"] is True
    assert s["first_layer_inspector"] is True
    # Sensors
    assert s["filament_tangle"] is True
    assert s["nozzle_blob"] is True
    # Door sensor — X1 family confirmed
    assert s["open_door_check"] is True
    # Behaviour
    assert s["auto_recovery"] is True
    assert s["sound"] is True
    # Parts
    assert s["parts_dual"] is False
    assert s["parts_editable"] is False


def test_a1_mini_no_ai_no_blob():
    s = _supports("A1 Mini")
    # A1 Mini has no AI camera-driven detectors
    assert s["spaghetti_detector"] is False
    assert s["nozzle_blob"] is False
    # Auto-recovery / sound — yes
    assert s["auto_recovery"] is True
    assert s["sound"] is True


def test_h2d_dual_parts():
    s = _supports("H2D")
    assert s["parts_dual"] is True
    # H2D supports AI
    assert s["spaghetti_detector"] is True


def test_h2d_pro_purify_air():
    s = _supports("H2D Pro")
    assert s["purify_air"] is True


def test_p1s_no_ai_no_door():
    s = _supports("P1S")
    assert s["spaghetti_detector"] is False
    assert s["open_door_check"] is False  # P1S has door but no MQTT bit
    assert s["auto_recovery"] is True


def test_unknown_model_safe_defaults():
    s = _supports("DefinitelyNotABambu")
    # Universal flags stay on
    assert s["auto_recovery"] is True
    assert s["sound"] is True
    # Camera/AI off
    assert s["spaghetti_detector"] is False
    # Dual off
    assert s["parts_dual"] is False


def test_all_supports_keys_present():
    s = _supports("X1C")
    for key in (
        "spaghetti_detector", "pileup_detector", "nozzleclumping_detector",
        "airprinting_detector", "first_layer_inspector", "ai_monitoring",
        "filament_tangle", "nozzle_blob", "fod_check", "displacement_detection",
        "open_door_check", "purify_air",
        "auto_recovery", "sound", "save_remote_to_storage", "snapshot",
        "plate_type", "plate_align",
        "parts_editable", "parts_dual",
    ):
        assert key in s, f"missing key: {key}"
```

- [ ] **Step 2: Run — confirm fail**

Run: `pytest backend/tests/unit/services/test_printer_capabilities.py -v`
Expected: ImportError.

- [ ] **Step 3: Write the helper**

```python
# backend/app/services/printer_capabilities.py
"""Per-model capability map for the Printer Settings dialog.

Mirrors the BS PrintOptionsDialog visibility rules. AI/visual detectors
sit behind camera-capable families (X1 + H2D); sensors like filament-
tangle live on X1/H2D; behaviour toggles (auto-recovery, sound) are
universal. Dual-nozzle parts editing only on H2D family.

This is intentionally a flat dict (no nested supports) so the API
response keeps it cheap to serialize and the frontend hides rows with
``!supports[key]`` checks.
"""

from typing import TypedDict

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.utils.printer_models import has_door_sensor


class PrinterSupports(TypedDict):
    # AI / visual detectors
    spaghetti_detector: bool
    pileup_detector: bool
    nozzleclumping_detector: bool
    airprinting_detector: bool
    first_layer_inspector: bool
    ai_monitoring: bool
    # Other sensors
    filament_tangle: bool
    nozzle_blob: bool
    fod_check: bool
    displacement_detection: bool
    # Door / air
    open_door_check: bool
    purify_air: bool
    # Behaviour
    auto_recovery: bool
    sound: bool
    save_remote_to_storage: bool
    snapshot: bool
    # Build plate
    plate_type: bool
    plate_align: bool
    # Parts
    parts_editable: bool
    parts_dual: bool


def _norm(model: str | None) -> str:
    if not model:
        return ""
    return model.strip().upper().replace(" ", "").replace("-", "")


_X1_FAMILY = frozenset({"X1", "X1C", "X1E"})
_H2_FAMILY = frozenset({"H2D", "H2DPRO", "H2C", "H2S"})
_AI_CAPABLE = _X1_FAMILY | _H2_FAMILY


def compute_printer_supports(
    state: PrinterState, printer_model: str | None, module_vers: dict
) -> PrinterSupports:
    m = _norm(printer_model)
    has_ai = m in _AI_CAPABLE
    is_h2 = m in _H2_FAMILY
    is_h2d_pro = m == "H2DPRO"

    return PrinterSupports(
        # AI detectors
        spaghetti_detector=has_ai,
        pileup_detector=has_ai,
        nozzleclumping_detector=has_ai,
        airprinting_detector=has_ai,
        first_layer_inspector=has_ai,
        ai_monitoring=has_ai,
        # Sensors
        filament_tangle=has_ai,
        nozzle_blob=m in _X1_FAMILY,            # BS gates this to X1 only
        fod_check=has_ai,
        displacement_detection=has_ai,
        # Door / air
        open_door_check=has_door_sensor(printer_model),
        purify_air=is_h2d_pro,
        # Behaviour — universal where MQTT supports it
        auto_recovery=True,
        sound=True,
        save_remote_to_storage=True,             # printers with SD-card storage; treat as universal here
        snapshot=has_ai,                         # snapshot follows camera-presence
        # Build plate
        plate_type=is_h2 or m in {"X2D", "P2S"},
        plate_align=is_h2 or m in {"X2D"},
        # Parts
        parts_editable=False,                    # read-only this iteration
        parts_dual=is_h2 and m in {"H2D", "H2DPRO"},
    )
```

- [ ] **Step 4: Run — confirm pass**

Run: `pytest backend/tests/unit/services/test_printer_capabilities.py -v`
Expected: 7 PASSED.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/printer_capabilities.py \
        backend/tests/unit/services/test_printer_capabilities.py
git commit -m "feat(printer): compute_printer_supports per-model capability helper"
```

---

## Task 8: Pydantic schemas `printer_settings.py`

**Files:**
- Create: `backend/app/schemas/printer_settings.py`

- [ ] **Step 1: Write schemas**

```python
# backend/app/schemas/printer_settings.py
"""Request/response schemas for /printers/{id}/settings."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


# ---------------- Response ----------------

class AiDetectorState(BaseModel):
    enabled: bool | None = None
    sensitivity: str | None = None


class PrintOptionsState(BaseModel):
    auto_recovery: bool | None = None
    sound: bool | None = None
    filament_tangle: bool | None = None
    nozzle_blob: bool | None = None
    save_remote_to_storage: int | None = None
    purify_air: int | None = None
    open_door: int | None = None
    plate_type: bool | None = None
    plate_align: bool | None = None
    snapshot: bool | None = None
    # AI detectors
    spaghetti_detector: AiDetectorState = AiDetectorState()
    pileup_detector: AiDetectorState = AiDetectorState()
    nozzleclumping_detector: AiDetectorState = AiDetectorState()
    airprinting_detector: AiDetectorState = AiDetectorState()
    first_layer_inspector: AiDetectorState = AiDetectorState()
    ai_monitoring: AiDetectorState = AiDetectorState()
    fod_check: bool | None = None
    displacement_detection: bool | None = None


class NozzleInfoOut(BaseModel):
    id: int
    type: str | None = None
    diameter: float | None = None
    flow_type: str | None = None


class PartsState(BaseModel):
    nozzles: list[NozzleInfoOut] = []


class PrinterSettingsSupports(BaseModel):
    # AI / detectors
    spaghetti_detector: bool = False
    pileup_detector: bool = False
    nozzleclumping_detector: bool = False
    airprinting_detector: bool = False
    first_layer_inspector: bool = False
    ai_monitoring: bool = False
    filament_tangle: bool = False
    nozzle_blob: bool = False
    fod_check: bool = False
    displacement_detection: bool = False
    open_door_check: bool = False
    purify_air: bool = False
    auto_recovery: bool = False
    sound: bool = False
    save_remote_to_storage: bool = False
    snapshot: bool = False
    plate_type: bool = False
    plate_align: bool = False
    parts_editable: bool = False
    parts_dual: bool = False


class PrinterSettingsGetResponse(BaseModel):
    print_options: PrintOptionsState
    parts: PartsState
    supports: PrinterSettingsSupports


# ---------------- POST body — discriminated union ----------------


class PrintOptionBoolAction(BaseModel):
    action: Literal["print_option_bool"]
    key: Literal[
        "auto_recovery", "sound", "filament_tangle", "nozzle_blob",
        "plate_type", "plate_align",
    ]
    enabled: bool


class PrintOptionIntAction(BaseModel):
    action: Literal["print_option_int"]
    key: Literal["save_remote_to_storage", "purify_air", "open_door"]
    value: int = Field(ge=0, le=10)


class XCamControlAction(BaseModel):
    action: Literal["xcam_control"]
    module: Literal[
        "first_layer_inspector", "spaghetti_detector",
        "purgechutepileup_detector", "nozzleclumping_detector",
        "airprinting_detector", "fod_check", "displacement_detection",
        "ai_monitoring",
    ]
    enabled: bool
    sensitivity: Literal["low", "medium", "high"] | None = None


class CameraSnapshotAction(BaseModel):
    action: Literal["camera_snapshot"]
    enabled: bool


class SetNozzleAction(BaseModel):
    # Phase-2 stub — backend returns 409 parts_not_editable.
    action: Literal["set_nozzle"]
    nozzle_id: int = Field(ge=0, le=1)
    type: str
    diameter: float
    flow_type: str


PrinterSettingsPostBody = Annotated[
    PrintOptionBoolAction | PrintOptionIntAction | XCamControlAction
    | CameraSnapshotAction | SetNozzleAction,
    Field(discriminator="action"),
]


class PrinterSettingsPostResponse(BaseModel):
    ok: bool
    sequence_id: str | None = None
```

- [ ] **Step 2: Smoke-import**

Run: `python -c "from backend.app.schemas.printer_settings import PrinterSettingsGetResponse, PrintOptionBoolAction, XCamControlAction; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add backend/app/schemas/printer_settings.py
git commit -m "feat(schemas): printer settings request/response models"
```

---

## Task 9: API router `printer_settings.py`

**Files:**
- Create: `backend/app/api/routes/printer_settings.py`
- Modify: `backend/app/main.py` — register router.
- Create: `backend/tests/integration/test_printer_settings_routes.py`

- [ ] **Step 1: Write integration tests**

```python
# backend/tests/integration/test_printer_settings_routes.py
"""Integration tests for /printers/{id}/settings GET + POST."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.printer_setting_audit import PrinterSettingAudit


def _client_with_state(printer_model="X1C", connected=True):
    c = MagicMock()
    c.state.connected = connected
    c.state.print_options = MagicMock()
    c.state.print_options.auto_recovery_step_loss = True
    c.state.print_options.sound_enable = None
    c.state.print_options.filament_tangle_detect = None
    c.state.print_options.nozzle_blob_detect = None
    c.state.print_options.air_purification = None
    c.state.print_options.open_door_check = None
    c.state.print_options.save_remote_to_storage = None
    c.state.print_options.plate_type_detect = None
    c.state.print_options.plate_align_check = None
    c.state.print_options.snapshot_enabled = None
    c.state.print_options.fod_check = None
    c.state.print_options.displacement_detection = None
    c.state.print_options.spaghetti_detector = True
    c.state.print_options.halt_print_sensitivity = "medium"
    c.state.print_options.nozzle_clumping_detector = False
    c.state.print_options.nozzle_clumping_sensitivity = "medium"
    c.state.print_options.pileup_detector = False
    c.state.print_options.pileup_sensitivity = "medium"
    c.state.print_options.airprint_detector = False
    c.state.print_options.airprint_sensitivity = "medium"
    c.state.print_options.first_layer_inspector = False
    c.state.print_options.printing_monitor = False
    c.state.nozzles = []
    c.module_vers = {}
    return c


@pytest.mark.asyncio
async def test_get_requires_read_perm(async_client, printer_factory):
    printer = await printer_factory(model="X1C")
    # No mock — printer_manager.get_client returns None → 404 not_online
    with patch("backend.app.api.routes.printer_settings.printer_manager") as pm:
        pm.get_client.return_value = None
        r = await async_client.get(f"/api/v1/printers/{printer.id}/settings")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_returns_state_and_supports(async_client, printer_factory):
    printer = await printer_factory(model="X1C")
    with patch("backend.app.api.routes.printer_settings.printer_manager") as pm:
        pm.get_client.return_value = _client_with_state("X1C")
        r = await async_client.get(f"/api/v1/printers/{printer.id}/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["print_options"]["auto_recovery"] is True
    assert body["supports"]["spaghetti_detector"] is True
    assert body["supports"]["parts_dual"] is False


@pytest.mark.asyncio
async def test_post_bool_action_publishes_and_audits(async_client, printer_factory, db_session):
    printer = await printer_factory(model="X1C")
    client = _client_with_state("X1C")
    client.print_option_auto_recovery = MagicMock(return_value=(True, "SEQ-PS-1"))

    with patch("backend.app.api.routes.printer_settings.printer_manager") as pm:
        pm.get_client.return_value = client
        r = await async_client.post(
            f"/api/v1/printers/{printer.id}/settings",
            json={"action": "print_option_bool", "key": "auto_recovery", "enabled": False},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["sequence_id"] == "SEQ-PS-1"
    client.print_option_auto_recovery.assert_called_once_with(False)

    rows = (
        await db_session.execute(
            select(PrinterSettingAudit).where(PrinterSettingAudit.printer_id == printer.id)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].action == "print_option_bool"
    assert rows[0].tab == "print_options"
    assert rows[0].result == "sent"


@pytest.mark.asyncio
async def test_post_xcam_control_with_sensitivity(async_client, printer_factory):
    printer = await printer_factory(model="X1C")
    client = _client_with_state("X1C")
    client.xcam_control_for_settings = MagicMock(return_value=(True, "SEQ-PS-2"))
    with patch("backend.app.api.routes.printer_settings.printer_manager") as pm:
        pm.get_client.return_value = client
        r = await async_client.post(
            f"/api/v1/printers/{printer.id}/settings",
            json={
                "action": "xcam_control",
                "module": "spaghetti_detector",
                "enabled": True,
                "sensitivity": "high",
            },
        )
    assert r.status_code == 200
    client.xcam_control_for_settings.assert_called_once_with(
        "spaghetti_detector", enabled=True, sensitivity="high"
    )


@pytest.mark.asyncio
async def test_post_unsupported_returns_409(async_client, printer_factory):
    printer = await printer_factory(model="A1 Mini")  # no AI
    client = _client_with_state("A1 Mini")
    with patch("backend.app.api.routes.printer_settings.printer_manager") as pm:
        pm.get_client.return_value = client
        r = await async_client.post(
            f"/api/v1/printers/{printer.id}/settings",
            json={
                "action": "xcam_control",
                "module": "spaghetti_detector",
                "enabled": True,
                "sensitivity": "medium",
            },
        )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_post_set_nozzle_returns_409_parts_not_editable(async_client, printer_factory):
    printer = await printer_factory(model="X1C")
    client = _client_with_state("X1C")
    with patch("backend.app.api.routes.printer_settings.printer_manager") as pm:
        pm.get_client.return_value = client
        r = await async_client.post(
            f"/api/v1/printers/{printer.id}/settings",
            json={
                "action": "set_nozzle", "nozzle_id": 0,
                "type": "stainless_steel", "diameter": 0.4, "flow_type": "standard",
            },
        )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_post_mqtt_failure_returns_504_and_audits_error(
    async_client, printer_factory, db_session
):
    printer = await printer_factory(model="X1C")
    client = _client_with_state("X1C")
    client.print_option_auto_recovery = MagicMock(return_value=(False, None))
    with patch("backend.app.api.routes.printer_settings.printer_manager") as pm:
        pm.get_client.return_value = client
        r = await async_client.post(
            f"/api/v1/printers/{printer.id}/settings",
            json={"action": "print_option_bool", "key": "auto_recovery", "enabled": True},
        )
    assert r.status_code == 504
    rows = (
        await db_session.execute(
            select(PrinterSettingAudit).where(PrinterSettingAudit.printer_id == printer.id)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].result == "error"
```

- [ ] **Step 2: Run — confirm fails (router missing)**

Run: `pytest backend/tests/integration/test_printer_settings_routes.py -v`
Expected: 404 on every call (route not registered).

- [ ] **Step 3: Write the router**

```python
# backend/app/api/routes/printer_settings.py
"""GET / POST /printers/{printer_id}/settings — Printer Settings dialog backend.

Mirrors BS PrintOptionsDialog + PrinterPartsDialog. State pulled from
PrinterState, writes routed through bambu_mqtt publishers. Every applied
POST writes a printer_setting_audit row (m061).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.printer import Printer
from backend.app.models.printer_setting_audit import PrinterSettingAudit
from backend.app.models.user import User
from backend.app.schemas.printer_settings import (
    AiDetectorState,
    CameraSnapshotAction,
    NozzleInfoOut,
    PartsState,
    PrintOptionBoolAction,
    PrintOptionIntAction,
    PrinterSettingsGetResponse,
    PrinterSettingsPostBody,
    PrinterSettingsPostResponse,
    PrinterSettingsSupports,
    PrintOptionsState,
    SetNozzleAction,
    XCamControlAction,
)
from backend.app.services.printer_capabilities import compute_printer_supports
from backend.app.services.printer_manager import printer_manager

router = APIRouter(prefix="/printers", tags=["printer-settings"])

# Map keys/modules → MQTT method on the client.
_BOOL_KEY_METHODS = {
    "auto_recovery": "print_option_auto_recovery",
    "sound": "print_option_sound",
    "filament_tangle": "print_option_filament_tangle",
    "nozzle_blob": "print_option_nozzle_blob",
    "plate_type": "print_option_plate_type",
    "plate_align": "print_option_plate_align",
}
_INT_KEY_METHODS = {
    "purify_air": "print_option_purify_air",
    "open_door": "print_option_open_door",
    "save_remote_to_storage": "print_option_save_remote_to_storage",
}
_BOOL_KEY_SUPPORTS = {
    "auto_recovery": "auto_recovery",
    "sound": "sound",
    "filament_tangle": "filament_tangle",
    "nozzle_blob": "nozzle_blob",
    "plate_type": "plate_type",
    "plate_align": "plate_align",
}
_INT_KEY_SUPPORTS = {
    "purify_air": "purify_air",
    "open_door": "open_door_check",
    "save_remote_to_storage": "save_remote_to_storage",
}
_XCAM_MODULE_SUPPORTS = {
    "spaghetti_detector": "spaghetti_detector",
    "purgechutepileup_detector": "pileup_detector",
    "nozzleclumping_detector": "nozzleclumping_detector",
    "airprinting_detector": "airprinting_detector",
    "first_layer_inspector": "first_layer_inspector",
    "ai_monitoring": "ai_monitoring",
    "fod_check": "fod_check",
    "displacement_detection": "displacement_detection",
}


def _action_tab(action: str) -> str:
    if action in {"print_option_bool", "print_option_int", "xcam_control", "camera_snapshot"}:
        return "print_options"
    if action == "set_nozzle":
        return "parts"
    return "unknown"


@router.get("/{printer_id}/settings", response_model=PrinterSettingsGetResponse)
async def get_printer_settings(
    printer_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_READ),
) -> PrinterSettingsGetResponse:
    printer = (
        await db.execute(select(Printer).where(Printer.id == printer_id))
    ).scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    client = printer_manager.get_client(printer_id)
    if not client or not client.state.connected:
        raise HTTPException(404, "Printer not online")

    po = client.state.print_options
    state = PrintOptionsState(
        auto_recovery=getattr(po, "auto_recovery_step_loss", None),
        sound=getattr(po, "sound_enable", None),
        filament_tangle=getattr(po, "filament_tangle_detect", None),
        nozzle_blob=getattr(po, "nozzle_blob_detect", None),
        save_remote_to_storage=getattr(po, "save_remote_to_storage", None),
        purify_air=getattr(po, "air_purification", None),
        open_door=getattr(po, "open_door_check", None),
        plate_type=getattr(po, "plate_type_detect", None),
        plate_align=getattr(po, "plate_align_check", None),
        snapshot=getattr(po, "snapshot_enabled", None),
        fod_check=getattr(po, "fod_check", None),
        displacement_detection=getattr(po, "displacement_detection", None),
        spaghetti_detector=AiDetectorState(
            enabled=getattr(po, "spaghetti_detector", None),
            sensitivity=getattr(po, "halt_print_sensitivity", None),
        ),
        pileup_detector=AiDetectorState(
            enabled=getattr(po, "pileup_detector", None),
            sensitivity=getattr(po, "pileup_sensitivity", None),
        ),
        nozzleclumping_detector=AiDetectorState(
            enabled=getattr(po, "nozzle_clumping_detector", None),
            sensitivity=getattr(po, "nozzle_clumping_sensitivity", None),
        ),
        airprinting_detector=AiDetectorState(
            enabled=getattr(po, "airprint_detector", None),
            sensitivity=getattr(po, "airprint_sensitivity", None),
        ),
        first_layer_inspector=AiDetectorState(
            enabled=getattr(po, "first_layer_inspector", None),
            sensitivity=None,
        ),
        ai_monitoring=AiDetectorState(
            enabled=getattr(po, "printing_monitor", None),
            sensitivity=None,
        ),
    )

    nozzles = []
    for idx, n in enumerate(getattr(client.state, "nozzles", []) or []):
        nozzles.append(
            NozzleInfoOut(
                id=idx,
                type=getattr(n, "type", None) or getattr(n, "nozzle_type", None),
                diameter=getattr(n, "diameter", None) or getattr(n, "nozzle_diameter", None),
                flow_type=getattr(n, "flow_type", None) or getattr(n, "nozzle_flow_type", None),
            )
        )
    parts = PartsState(nozzles=nozzles)

    supports = PrinterSettingsSupports(
        **compute_printer_supports(
            client.state, printer.model, getattr(client, "module_vers", {})
        )
    )

    return PrinterSettingsGetResponse(
        print_options=state, parts=parts, supports=supports
    )


@router.post("/{printer_id}/settings", response_model=PrinterSettingsPostResponse)
async def post_printer_settings(
    printer_id: int,
    body: PrinterSettingsPostBody = Body(...),
    db: AsyncSession = Depends(get_db),
    user: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
) -> PrinterSettingsPostResponse:
    printer = (
        await db.execute(select(Printer).where(Printer.id == printer_id))
    ).scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    client = printer_manager.get_client(printer_id)
    if not client or not client.state.connected:
        raise HTTPException(404, "Printer not online")

    supports = compute_printer_supports(
        client.state, printer.model, getattr(client, "module_vers", {})
    )

    sequence_id: str | None = None
    error: str | None = None
    result_label = "sent"

    try:
        if isinstance(body, PrintOptionBoolAction):
            if not supports.get(_BOOL_KEY_SUPPORTS[body.key]):
                raise HTTPException(409, f"{body.key} not supported on this printer")
            method = getattr(client, _BOOL_KEY_METHODS[body.key])
            ok, sequence_id = method(body.enabled)

        elif isinstance(body, PrintOptionIntAction):
            if not supports.get(_INT_KEY_SUPPORTS[body.key]):
                raise HTTPException(409, f"{body.key} not supported on this printer")
            method = getattr(client, _INT_KEY_METHODS[body.key])
            ok, sequence_id = method(body.value)

        elif isinstance(body, XCamControlAction):
            if not supports.get(_XCAM_MODULE_SUPPORTS[body.module]):
                raise HTTPException(409, f"{body.module} not supported on this printer")
            ok, sequence_id = client.xcam_control_for_settings(
                body.module, enabled=body.enabled, sensitivity=body.sensitivity
            )

        elif isinstance(body, CameraSnapshotAction):
            if not supports.get("snapshot"):
                raise HTTPException(409, "snapshot not supported on this printer")
            ok, sequence_id = client.camera_snapshot_enable(body.enabled)

        elif isinstance(body, SetNozzleAction):
            raise HTTPException(409, "parts_not_editable")

        else:
            raise HTTPException(400, "unknown action")

        if not ok:
            result_label = "error"
            error = "MQTT publish failed"

    except HTTPException:
        raise
    except Exception as exc:
        result_label = "error"
        error = str(exc)

    db.add(
        PrinterSettingAudit(
            printer_id=printer_id,
            user_id=user.id if user else None,
            tab=_action_tab(body.action),
            action=body.action,
            payload_json=json.dumps(body.model_dump(mode="json")),
            sequence_id=sequence_id,
            result=result_label,
            error_message=error,
        )
    )
    await db.commit()

    if result_label == "error":
        raise HTTPException(504, error or "MQTT publish failed")

    return PrinterSettingsPostResponse(ok=True, sequence_id=sequence_id)
```

- [ ] **Step 4: Register router in `main.py`**

In `backend/app/main.py`, find the `from backend.app.api.routes import (...)` block. Add `printer_settings as printer_settings_routes,` near `ams_settings as ams_settings_routes,`. Then find the `app.include_router(ams_settings_routes.router, prefix=app_settings.api_prefix)` line and add this one directly after it:

```python
app.include_router(printer_settings_routes.router, prefix=app_settings.api_prefix)
```

- [ ] **Step 5: Run tests — confirm pass**

Run: `pytest backend/tests/integration/test_printer_settings_routes.py -v`
Expected: 7 PASSED.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/routes/printer_settings.py backend/app/main.py \
        backend/tests/integration/test_printer_settings_routes.py
git commit -m "feat(api): /printers/{id}/settings GET + POST with audit"
```

---

## Task 10: Frontend API client + hook

**Files:**
- Modify: `frontend/src/api/client.ts` — types + 2 methods.
- Create: `frontend/src/hooks/usePrinterSettings.ts`

- [ ] **Step 1: Add types and methods in client.ts**

Find the AMS Settings type block (search `AmsSettingsGetResponse`). Right after the `AmsSettingsPostResponse` interface, append:

```typescript
// Printer Settings dialog. Mirrors backend/app/schemas/printer_settings.py.
export interface AiDetectorStateOut {
  enabled: boolean | null;
  sensitivity: string | null;
}

export interface PrintOptionsState {
  auto_recovery: boolean | null;
  sound: boolean | null;
  filament_tangle: boolean | null;
  nozzle_blob: boolean | null;
  save_remote_to_storage: number | null;
  purify_air: number | null;
  open_door: number | null;
  plate_type: boolean | null;
  plate_align: boolean | null;
  snapshot: boolean | null;
  fod_check: boolean | null;
  displacement_detection: boolean | null;
  spaghetti_detector: AiDetectorStateOut;
  pileup_detector: AiDetectorStateOut;
  nozzleclumping_detector: AiDetectorStateOut;
  airprinting_detector: AiDetectorStateOut;
  first_layer_inspector: AiDetectorStateOut;
  ai_monitoring: AiDetectorStateOut;
}

export interface PrinterPartsState {
  nozzles: { id: number; type: string | null; diameter: number | null; flow_type: string | null }[];
}

export interface PrinterSettingsSupports {
  spaghetti_detector: boolean;
  pileup_detector: boolean;
  nozzleclumping_detector: boolean;
  airprinting_detector: boolean;
  first_layer_inspector: boolean;
  ai_monitoring: boolean;
  filament_tangle: boolean;
  nozzle_blob: boolean;
  fod_check: boolean;
  displacement_detection: boolean;
  open_door_check: boolean;
  purify_air: boolean;
  auto_recovery: boolean;
  sound: boolean;
  save_remote_to_storage: boolean;
  snapshot: boolean;
  plate_type: boolean;
  plate_align: boolean;
  parts_editable: boolean;
  parts_dual: boolean;
}

export interface PrinterSettingsGetResponse {
  print_options: PrintOptionsState;
  parts: PrinterPartsState;
  supports: PrinterSettingsSupports;
}

export type PrinterSettingsPostBody =
  | { action: 'print_option_bool';
      key: 'auto_recovery' | 'sound' | 'filament_tangle' | 'nozzle_blob' | 'plate_type' | 'plate_align';
      enabled: boolean }
  | { action: 'print_option_int';
      key: 'save_remote_to_storage' | 'purify_air' | 'open_door';
      value: number }
  | { action: 'xcam_control';
      module: 'first_layer_inspector' | 'spaghetti_detector' | 'purgechutepileup_detector'
            | 'nozzleclumping_detector' | 'airprinting_detector' | 'fod_check'
            | 'displacement_detection' | 'ai_monitoring';
      enabled: boolean;
      sensitivity: 'low' | 'medium' | 'high' | null }
  | { action: 'camera_snapshot'; enabled: boolean }
  | { action: 'set_nozzle'; nozzle_id: number; type: string; diameter: number; flow_type: string };

export interface PrinterSettingsPostResponse {
  ok: boolean;
  sequence_id: string | null;
}
```

Then find the AMS Settings method block (search `getAmsSettings:`). Right after `postAmsSettings: ...`, append:

```typescript
  getPrinterSettings: (printerId: number) =>
    request<PrinterSettingsGetResponse>(`/printers/${printerId}/settings`),
  postPrinterSettings: (printerId: number, body: PrinterSettingsPostBody) =>
    request<PrinterSettingsPostResponse>(`/printers/${printerId}/settings`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
```

- [ ] **Step 2: Write the hook**

```typescript
// frontend/src/hooks/usePrinterSettings.ts
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useRef } from 'react';

import { api } from '../api/client';
import type { PrinterSettingsGetResponse, PrinterSettingsPostBody } from '../api/client';

const QK = (printerId: number) => ['printer-settings', printerId] as const;

/**
 * Printer Settings dialog data fetcher + mutator. Same shape as
 * useAmsSettings — per-flag 3 s client hold-timer mirrors the backend
 * hold so the UI doesn't blink between optimistic and confirmed values.
 */
export function usePrinterSettings(printerId: number, enabled: boolean = true) {
  const qc = useQueryClient();
  const holdsRef = useRef<Map<string, number>>(new Map());

  const query = useQuery<PrinterSettingsGetResponse>({
    queryKey: QK(printerId),
    queryFn: () => api.getPrinterSettings(printerId),
    enabled,
    staleTime: 5_000,
  });

  const mutation = useMutation({
    mutationFn: (body: PrinterSettingsPostBody) => api.postPrinterSettings(printerId, body),
    onSuccess: (_d, body) => {
      const now = Date.now();
      for (const flag of flagsForAction(body)) holdsRef.current.set(flag, now + 3_000);
      qc.invalidateQueries({ queryKey: QK(printerId) });
    },
  });

  const isHeld = useCallback((flag: string) => {
    const deadline = holdsRef.current.get(flag);
    return deadline != null && deadline > Date.now();
  }, []);

  return {
    data: query.data,
    isLoading: query.isLoading,
    error: query.error,
    refetch: query.refetch,
    mutate: mutation.mutateAsync,
    isMutating: mutation.isPending,
    isHeld,
  };
}

function flagsForAction(body: PrinterSettingsPostBody): string[] {
  if (body.action === 'print_option_bool') return [body.key];
  if (body.action === 'print_option_int') return [body.key];
  if (body.action === 'xcam_control') return [body.module];
  if (body.action === 'camera_snapshot') return ['snapshot'];
  return [];
}
```

- [ ] **Step 3: tsc smoke**

Run: `cd frontend && npx tsc --noEmit`
Expected: no new errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/client.ts frontend/src/hooks/usePrinterSettings.ts
git commit -m "feat(frontend): printer settings API client + usePrinterSettings hook"
```

---

## Task 11: i18n keys

**Files:**
- Modify: `frontend/src/i18n/locales/en.ts`
- Modify: `frontend/src/i18n/locales/uk.ts`

- [ ] **Step 1: Extract uk msgstrs from `BambuStudio_uk.po`**

For each English `msgid` below, find the matching `msgstr` in `temp/references/BambuStudio/bbl/i18n/uk/BambuStudio_uk.po`. Empty `msgstr` → BamDude-original Ukrainian translation (no TBD markers; same policy as the AMS Settings final state).

Keys to extract: `"Printer Settings"`, `"Calibration"` (already used in AMS Settings — reuse), `"AI Monitoring"`, `"Spaghetti detection"`, `"Purgechute pileup"`, `"Nozzle clumping"`, `"Air-printing detection"`, `"First layer inspection"`, `"Filament tangle detection"`, `"Nozzle blob detection"`, `"FOD check"`, `"Displacement detection"`, `"Open door check"`, `"Purify air at print end"`, `"Auto recovery from step loss"`, `"Prompt sound"`, `"Save remote print files to storage"`, `"Snapshot"`, `"Build plate marker detection"`, `"Plate alignment"`, `"Type"`, `"Diameter"`, `"Flow"`, `"Please change the nozzle settings on the printer."`, `"Refresh from printer"`, `"Low"`, `"Medium"`, `"High"`, `"Off"`.

- [ ] **Step 2: Append `printerSettings` block in en.ts**

Find the `amsSettings:` block (added during the AMS Settings dialog work). Directly after it (after the closing `},`), insert:

```typescript
  printerSettings: {
    menuItem: "Printer Settings",
    title: "Printer Settings",
    tab: {
      printOptions: "Print Options",
      parts: "Printer Parts",
    },
    aiMonitoringGroup: "AI Monitoring",
    sensorsGroup: "Sensors",
    doorAirGroup: "Door & Air",
    behaviourGroup: "Behaviour",
    buildPlateGroup: "Build Plate",
    spaghetti: "Spaghetti detection",
    pileup: "Purgechute pileup",
    clumping: "Nozzle clumping",
    airprint: "Air-printing detection",
    firstLayer: "First layer inspection",
    aiMonitoring: "AI monitoring",
    filamentTangle: "Filament tangle detection",
    nozzleBlob: "Nozzle blob detection",
    fodCheck: "FOD check",
    displacement: "Displacement detection",
    openDoorCheck: "Open door check",
    purifyAirEnd: "Purify air at print end",
    autoRecovery: "Auto recovery from step loss",
    sound: "Prompt sound",
    saveRemoteToStorage: "Save remote print files to storage",
    snapshot: "Snapshot",
    plateType: "Build plate marker detection",
    plateAlign: "Plate alignment",
    sensitivity: { low: "Low", medium: "Medium", high: "High" },
    openDoorMode: { off: "Off", pause: "Pause", halt: "Halt" },
    purifyAirMode: { off: "Off", inside: "Inside", outside: "Outside" },
    parts: {
      type: "Type",
      diameter: "Diameter",
      flow: "Flow",
      leftNozzle: "Left",
      rightNozzle: "Right",
      changeOnPrinter: "Please change the nozzle settings on the printer.",
      refresh: "Refresh from printer",
    },
    waitingForPrinter: "Waiting for printer status",
    requestFailed: "Request failed",
  },
```

- [ ] **Step 3: Append `printerSettings` block in uk.ts**

Same shape, Ukrainian values. Where `BambuStudio_uk.po` has an empty `msgstr`, supply our translation (no TBD markers). Example block (verify each line against the .po before committing — if it has a real translation, use it):

```typescript
  printerSettings: {
    menuItem: "Налаштування принтера",
    title: "Налаштування принтера",
    tab: {
      printOptions: "Параметри друку",
      parts: "Частини принтера",
    },
    aiMonitoringGroup: "AI-моніторинг",
    sensorsGroup: "Сенсори",
    doorAirGroup: "Двері та повітря",
    behaviourGroup: "Поведінка",
    buildPlateGroup: "Платформа",
    spaghetti: "Виявлення спагеті",
    pileup: "Накопичення в зливі",
    clumping: "Налипання на сопло",
    airprint: "Виявлення друку в повітрі",
    firstLayer: "Перевірка першого шару",
    aiMonitoring: "AI-моніторинг",
    filamentTangle: "Виявлення сплутування філаменту",
    nozzleBlob: "Виявлення натікання сопла",
    fodCheck: "Виявлення сторонніх об'єктів",
    displacement: "Виявлення зміщення",
    openDoorCheck: "Перевірка відкритих дверцят",
    purifyAirEnd: "Очищення повітря в кінці друку",
    autoRecovery: "Автоматичне відновлення після втрати кроку",
    sound: "Звуковий сигнал",
    saveRemoteToStorage: "Зберігати віддалені файли друку в сховище",
    snapshot: "Знімок",
    plateType: "Виявлення маркера платформи",
    plateAlign: "Вирівнювання платформи",
    sensitivity: { low: "Низька", medium: "Середня", high: "Висока" },
    openDoorMode: { off: "Вимк.", pause: "Пауза", halt: "Стоп" },
    purifyAirMode: { off: "Вимк.", inside: "Всередину", outside: "Назовні" },
    parts: {
      type: "Тип",
      diameter: "Діаметр",
      flow: "Потік",
      leftNozzle: "Ліве",
      rightNozzle: "Праве",
      changeOnPrinter: "Будь ласка, змініть налаштування сопла на принтері.",
      refresh: "Оновити з принтера",
    },
    waitingForPrinter: "Очікування статусу принтера",
    requestFailed: "Помилка запиту",
  },
```

- [ ] **Step 4: tsc + lint smoke**

Run: `cd frontend && npx tsc --noEmit && npm run lint`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/i18n/locales/en.ts frontend/src/i18n/locales/uk.ts
git commit -m "feat(i18n): printerSettings block en+uk"
```

---

## Task 12: `PrintOptionsTab` component

**Files:**
- Create: `frontend/src/components/PrintOptionsTab.tsx`

- [ ] **Step 1: Write the component**

```tsx
// frontend/src/components/PrintOptionsTab.tsx
import { useTranslation } from 'react-i18next';
import type {
  PrinterSettingsGetResponse,
  PrinterSettingsPostBody,
} from '../api/client';

interface Props {
  data: PrinterSettingsGetResponse;
  onSubmit: (body: PrinterSettingsPostBody) => Promise<void>;
}

export function PrintOptionsTab({ data, onSubmit }: Props) {
  const { t } = useTranslation();
  const s = data.print_options;
  const sup = data.supports;

  const sensitivityOptions = [
    { v: 'low', label: t('printerSettings.sensitivity.low') },
    { v: 'medium', label: t('printerSettings.sensitivity.medium') },
    { v: 'high', label: t('printerSettings.sensitivity.high') },
  ] as const;

  const toggleBool = (key: PrinterSettingsPostBody extends { action: 'print_option_bool'; key: infer K } ? K : never, next: boolean) =>
    onSubmit({ action: 'print_option_bool', key, enabled: next });

  const toggleInt = (key: 'save_remote_to_storage' | 'purify_air' | 'open_door', value: number) =>
    onSubmit({ action: 'print_option_int', key, value });

  const toggleXcam = (
    module:
      | 'first_layer_inspector' | 'spaghetti_detector' | 'purgechutepileup_detector'
      | 'nozzleclumping_detector' | 'airprinting_detector' | 'fod_check'
      | 'displacement_detection' | 'ai_monitoring',
    enabled: boolean,
    sensitivity: 'low' | 'medium' | 'high' | null,
  ) => onSubmit({ action: 'xcam_control', module, enabled, sensitivity });

  return (
    <div className="space-y-5">
      {(sup.spaghetti_detector || sup.pileup_detector || sup.nozzleclumping_detector || sup.airprinting_detector || sup.first_layer_inspector || sup.ai_monitoring) && (
        <Group title={t('printerSettings.aiMonitoringGroup')}>
          {sup.first_layer_inspector && (
            <XCamRow
              title={t('printerSettings.firstLayer')}
              state={s.first_layer_inspector}
              onChange={(en, sens) => toggleXcam('first_layer_inspector', en, sens)}
              sensitivityOptions={sensitivityOptions}
            />
          )}
          {sup.spaghetti_detector && (
            <XCamRow
              title={t('printerSettings.spaghetti')}
              state={s.spaghetti_detector}
              onChange={(en, sens) => toggleXcam('spaghetti_detector', en, sens)}
              sensitivityOptions={sensitivityOptions}
            />
          )}
          {sup.pileup_detector && (
            <XCamRow
              title={t('printerSettings.pileup')}
              state={s.pileup_detector}
              onChange={(en, sens) => toggleXcam('purgechutepileup_detector', en, sens)}
              sensitivityOptions={sensitivityOptions}
            />
          )}
          {sup.nozzleclumping_detector && (
            <XCamRow
              title={t('printerSettings.clumping')}
              state={s.nozzleclumping_detector}
              onChange={(en, sens) => toggleXcam('nozzleclumping_detector', en, sens)}
              sensitivityOptions={sensitivityOptions}
            />
          )}
          {sup.airprinting_detector && (
            <XCamRow
              title={t('printerSettings.airprint')}
              state={s.airprinting_detector}
              onChange={(en, sens) => toggleXcam('airprinting_detector', en, sens)}
              sensitivityOptions={sensitivityOptions}
            />
          )}
        </Group>
      )}

      {(sup.filament_tangle || sup.nozzle_blob || sup.fod_check || sup.displacement_detection) && (
        <Group title={t('printerSettings.sensorsGroup')}>
          {sup.filament_tangle && (
            <SimpleRow
              title={t('printerSettings.filamentTangle')}
              checked={!!s.filament_tangle}
              onChange={(v) => toggleBool('filament_tangle', v)}
            />
          )}
          {sup.nozzle_blob && (
            <SimpleRow
              title={t('printerSettings.nozzleBlob')}
              checked={!!s.nozzle_blob}
              onChange={(v) => toggleBool('nozzle_blob', v)}
            />
          )}
          {sup.fod_check && (
            <XCamRow
              title={t('printerSettings.fodCheck')}
              state={{ enabled: s.fod_check, sensitivity: null }}
              onChange={(en) => toggleXcam('fod_check', en, null)}
              sensitivityOptions={[]}
            />
          )}
          {sup.displacement_detection && (
            <XCamRow
              title={t('printerSettings.displacement')}
              state={{ enabled: s.displacement_detection, sensitivity: null }}
              onChange={(en) => toggleXcam('displacement_detection', en, null)}
              sensitivityOptions={[]}
            />
          )}
        </Group>
      )}

      {(sup.open_door_check || sup.purify_air) && (
        <Group title={t('printerSettings.doorAirGroup')}>
          {sup.open_door_check && (
            <SegmentedRow
              title={t('printerSettings.openDoorCheck')}
              value={s.open_door ?? 0}
              options={[
                { v: 0, label: t('printerSettings.openDoorMode.off') },
                { v: 1, label: t('printerSettings.openDoorMode.pause') },
                { v: 2, label: t('printerSettings.openDoorMode.halt') },
              ]}
              onChange={(v) => toggleInt('open_door', v)}
            />
          )}
          {sup.purify_air && (
            <SegmentedRow
              title={t('printerSettings.purifyAirEnd')}
              value={s.purify_air ?? 0}
              options={[
                { v: 0, label: t('printerSettings.purifyAirMode.off') },
                { v: 1, label: t('printerSettings.purifyAirMode.inside') },
                { v: 2, label: t('printerSettings.purifyAirMode.outside') },
              ]}
              onChange={(v) => toggleInt('purify_air', v)}
            />
          )}
        </Group>
      )}

      <Group title={t('printerSettings.behaviourGroup')}>
        {sup.auto_recovery && (
          <SimpleRow
            title={t('printerSettings.autoRecovery')}
            checked={!!s.auto_recovery}
            onChange={(v) => toggleBool('auto_recovery', v)}
          />
        )}
        {sup.sound && (
          <SimpleRow
            title={t('printerSettings.sound')}
            checked={!!s.sound}
            onChange={(v) => toggleBool('sound', v)}
          />
        )}
        {sup.save_remote_to_storage && (
          <SimpleRow
            title={t('printerSettings.saveRemoteToStorage')}
            checked={(s.save_remote_to_storage ?? 0) > 0}
            onChange={(v) => toggleInt('save_remote_to_storage', v ? 1 : 0)}
          />
        )}
        {sup.snapshot && (
          <SimpleRow
            title={t('printerSettings.snapshot')}
            checked={!!s.snapshot}
            onChange={(v) => onSubmit({ action: 'camera_snapshot', enabled: v })}
          />
        )}
      </Group>

      {(sup.plate_type || sup.plate_align) && (
        <Group title={t('printerSettings.buildPlateGroup')}>
          {sup.plate_type && (
            <SimpleRow
              title={t('printerSettings.plateType')}
              checked={!!s.plate_type}
              onChange={(v) => toggleBool('plate_type', v)}
            />
          )}
          {sup.plate_align && (
            <SimpleRow
              title={t('printerSettings.plateAlign')}
              checked={!!s.plate_align}
              onChange={(v) => toggleBool('plate_align', v)}
            />
          )}
        </Group>
      )}
    </div>
  );
}

function Group({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="border-t border-bambu-dark-tertiary pt-3 first:border-t-0 first:pt-0">
      <div className="text-xs uppercase tracking-wider text-bambu-gray mb-2">{title}</div>
      <div className="space-y-2">{children}</div>
    </div>
  );
}

function SimpleRow({ title, checked, onChange }: { title: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex items-center gap-3 cursor-pointer">
      <input
        type="checkbox"
        className="h-4 w-4 accent-bambu-green"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        aria-label={title}
      />
      <span className="text-white">{title}</span>
    </label>
  );
}

function XCamRow({
  title,
  state,
  onChange,
  sensitivityOptions,
}: {
  title: string;
  state: { enabled: boolean | null; sensitivity: string | null };
  onChange: (enabled: boolean, sensitivity: 'low' | 'medium' | 'high' | null) => void;
  sensitivityOptions: readonly { v: string; label: string }[];
}) {
  const enabled = !!state.enabled;
  const sens = (state.sensitivity as 'low' | 'medium' | 'high' | null) ?? 'medium';
  return (
    <label className="flex items-center gap-3 cursor-pointer">
      <input
        type="checkbox"
        className="h-4 w-4 accent-bambu-green"
        checked={enabled}
        onChange={(e) => onChange(e.target.checked, sensitivityOptions.length ? sens : null)}
        aria-label={title}
      />
      <span className="text-white flex-1">{title}</span>
      {sensitivityOptions.length > 0 && (
        <select
          className="bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1 text-white text-sm"
          value={sens}
          disabled={!enabled}
          onChange={(e) => onChange(enabled, e.target.value as 'low' | 'medium' | 'high')}
        >
          {sensitivityOptions.map((o) => (
            <option key={o.v} value={o.v}>{o.label}</option>
          ))}
        </select>
      )}
    </label>
  );
}

function SegmentedRow({
  title,
  value,
  options,
  onChange,
}: {
  title: string;
  value: number;
  options: { v: number; label: string }[];
  onChange: (v: number) => void;
}) {
  return (
    <div>
      <div className="text-white text-sm mb-1">{title}</div>
      <div className="inline-flex gap-1 rounded-lg p-1 bg-bambu-dark">
        {options.map((o) => (
          <button
            key={o.v}
            type="button"
            className={`px-3 py-1 text-sm rounded-md transition-colors ${
              value === o.v ? 'bg-bambu-green text-white' : 'text-bambu-gray hover:text-white'
            }`}
            onClick={() => onChange(o.v)}
          >
            {o.label}
          </button>
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: tsc smoke**

Run: `cd frontend && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/PrintOptionsTab.tsx
git commit -m "feat(frontend): PrintOptionsTab — grouped toggles + sensitivity + segmented"
```

---

## Task 13: `PrinterPartsTab` component

**Files:**
- Create: `frontend/src/components/PrinterPartsTab.tsx`

- [ ] **Step 1: Write the component**

```tsx
// frontend/src/components/PrinterPartsTab.tsx
import { useTranslation } from 'react-i18next';

import type { PrinterSettingsGetResponse } from '../api/client';

interface Props {
  data: PrinterSettingsGetResponse;
  onRefetch: () => void;
}

export function PrinterPartsTab({ data, onRefetch }: Props) {
  const { t } = useTranslation();
  const nozzles = data.parts.nozzles ?? [];
  const dual = data.supports.parts_dual;

  if (nozzles.length === 0) {
    return <div className="text-bambu-gray">{t('printerSettings.waitingForPrinter')}</div>;
  }

  return (
    <div className="space-y-4">
      <div className={dual ? 'grid grid-cols-2 gap-6' : ''}>
        {nozzles.map((n) => (
          <NozzleCard
            key={n.id}
            label={dual ? (n.id === 0 ? t('printerSettings.parts.leftNozzle') : t('printerSettings.parts.rightNozzle')) : null}
            type={n.type}
            diameter={n.diameter}
            flowType={n.flow_type}
          />
        ))}
      </div>
      <p className="text-sm text-bambu-gray">
        {t('printerSettings.parts.changeOnPrinter')}
      </p>
      <button
        type="button"
        className="px-3 py-1 bg-bambu-dark hover:bg-bambu-dark-tertiary rounded text-white text-sm"
        onClick={onRefetch}
      >
        {t('printerSettings.parts.refresh')}
      </button>
    </div>
  );
}

function NozzleCard({ label, type, diameter, flowType }: {
  label: string | null;
  type: string | null;
  diameter: number | null;
  flowType: string | null;
}) {
  const { t } = useTranslation();
  return (
    <div className="space-y-2">
      {label && <div className="text-white font-medium">{label}</div>}
      <ReadOnlyRow label={t('printerSettings.parts.type')} value={type ?? '—'} />
      <ReadOnlyRow label={t('printerSettings.parts.diameter')} value={diameter != null ? String(diameter) : '—'} />
      <ReadOnlyRow label={t('printerSettings.parts.flow')} value={flowType ?? '—'} />
    </div>
  );
}

function ReadOnlyRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center gap-3">
      <div className="text-bambu-gray text-sm w-24">{label}</div>
      <div className="flex-1 bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1 text-white opacity-70">
        {value}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: tsc smoke**

Run: `cd frontend && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/PrinterPartsTab.tsx
git commit -m "feat(frontend): PrinterPartsTab — read-only nozzle info"
```

---

## Task 14: `PrinterSettingsModal` component + vitest

**Files:**
- Create: `frontend/src/components/PrinterSettingsModal.tsx`
- Create: `frontend/src/__tests__/components/PrinterSettingsModal.test.tsx`

- [ ] **Step 1: Write component tests**

```tsx
// frontend/src/__tests__/components/PrinterSettingsModal.test.tsx
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { PrinterSettingsModal } from '../../components/PrinterSettingsModal';
import { api } from '../../api/client';

vi.mock('../../api/client', () => ({
  setAuthToken: vi.fn(),
  getAuthToken: vi.fn(() => 'test-admin-token'),
  api: {
    getPrinterSettings: vi.fn(),
    postPrinterSettings: vi.fn(),
    // Wrapper providers
    getSettings: vi.fn().mockResolvedValue({}),
    updateSettings: vi.fn().mockResolvedValue({}),
    getCurrentUser: vi.fn().mockResolvedValue({
      id: 1, username: 'admin', role: 'admin',
      permissions: ['printers:read', 'printers:update'],
    }),
    getAuthStatus: vi.fn().mockResolvedValue({ setup_required: false, authenticated: true }),
  },
}));

const fullSupports = {
  spaghetti_detector: true, pileup_detector: true, nozzleclumping_detector: true,
  airprinting_detector: true, first_layer_inspector: true, ai_monitoring: true,
  filament_tangle: true, nozzle_blob: true, fod_check: true, displacement_detection: true,
  open_door_check: true, purify_air: false,
  auto_recovery: true, sound: true, save_remote_to_storage: true, snapshot: true,
  plate_type: false, plate_align: false,
  parts_editable: false, parts_dual: false,
};

const baseState = {
  auto_recovery: true, sound: false, filament_tangle: true, nozzle_blob: false,
  save_remote_to_storage: null, purify_air: null, open_door: 0,
  plate_type: null, plate_align: null, snapshot: false,
  fod_check: false, displacement_detection: false,
  spaghetti_detector: { enabled: true, sensitivity: 'medium' },
  pileup_detector: { enabled: false, sensitivity: 'medium' },
  nozzleclumping_detector: { enabled: false, sensitivity: 'medium' },
  airprinting_detector: { enabled: false, sensitivity: 'medium' },
  first_layer_inspector: { enabled: false, sensitivity: null },
  ai_monitoring: { enabled: false, sensitivity: null },
};

beforeEach(() => {
  vi.clearAllMocks();
  (api.getPrinterSettings as ReturnType<typeof vi.fn>).mockResolvedValue({
    print_options: baseState,
    parts: { nozzles: [{ id: 0, type: 'stainless_steel', diameter: 0.4, flow_type: 'standard' }] },
    supports: fullSupports,
  });
  (api.postPrinterSettings as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, sequence_id: 'S1' });
});

describe('PrinterSettingsModal', () => {
  it('returns null when closed', () => {
    render(<PrinterSettingsModal isOpen={false} onClose={() => {}} printerId={1} />);
    expect(screen.queryByText('Printer Settings')).toBeNull();
  });

  it('renders both tabs and switches between them', async () => {
    render(<PrinterSettingsModal isOpen={true} onClose={() => {}} printerId={1} />);
    await waitFor(() => expect(screen.getByText('Print Options')).toBeInTheDocument());
    expect(screen.getByText('Printer Parts')).toBeInTheDocument();
    // Default tab: Print Options
    expect(screen.getByText('Auto recovery from step loss')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Printer Parts'));
    expect(screen.getByText('Type')).toBeInTheDocument();
  });

  it('toggling Auto recovery sends print_option_bool', async () => {
    render(<PrinterSettingsModal isOpen={true} onClose={() => {}} printerId={1} />);
    const cb = await screen.findByLabelText('Auto recovery from step loss');
    fireEvent.click(cb);
    await waitFor(() => {
      expect(api.postPrinterSettings).toHaveBeenCalledWith(1, {
        action: 'print_option_bool', key: 'auto_recovery', enabled: false,
      });
    });
  });

  it('hides unsupported rows', async () => {
    (api.getPrinterSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      print_options: baseState,
      parts: { nozzles: [] },
      supports: { ...fullSupports, spaghetti_detector: false, nozzle_blob: false },
    });
    render(<PrinterSettingsModal isOpen={true} onClose={() => {}} printerId={1} />);
    await waitFor(() => expect(screen.getByText('Auto recovery from step loss')).toBeInTheDocument());
    expect(screen.queryByText('Spaghetti detection')).toBeNull();
    expect(screen.queryByText('Nozzle blob detection')).toBeNull();
  });
});
```

- [ ] **Step 2: Run — confirm fails (component missing)**

Run: `cd frontend && npm run test:run -- PrinterSettingsModal`
Expected: import errors.

- [ ] **Step 3: Write the modal**

```tsx
// frontend/src/components/PrinterSettingsModal.tsx
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';

import { useToast } from '../contexts/ToastContext';
import { usePrinterSettings } from '../hooks/usePrinterSettings';
import { PrintOptionsTab } from './PrintOptionsTab';
import { PrinterPartsTab } from './PrinterPartsTab';
import type { PrinterSettingsPostBody } from '../api/client';

type TabId = 'print_options' | 'parts';

interface Props {
  isOpen: boolean;
  onClose: () => void;
  printerId: number;
}

export function PrinterSettingsModal({ isOpen, onClose, printerId }: Props) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const { data, isLoading, mutate, refetch } = usePrinterSettings(printerId, isOpen);
  const [activeTab, setActiveTab] = useState<TabId>('print_options');

  if (!isOpen) return null;

  const onSubmit = async (body: PrinterSettingsPostBody) => {
    try {
      await mutate(body);
    } catch (e) {
      showToast((e as Error)?.message ?? t('printerSettings.requestFailed'), 'error');
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-2xl w-full max-w-2xl mx-4 max-h-[90vh] overflow-y-auto">
        <div className="flex justify-between items-center p-4 border-b border-bambu-dark-tertiary">
          <h2 className="text-lg font-semibold text-white">{t('printerSettings.title')}</h2>
          <button
            onClick={onClose}
            aria-label="Close"
            className="p-1 text-bambu-gray hover:text-white rounded transition-colors"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="px-4 pt-3">
          <div className="inline-flex gap-1 rounded-lg p-1 bg-bambu-dark">
            <TabBtn id="print_options" active={activeTab} onClick={setActiveTab}>
              {t('printerSettings.tab.printOptions')}
            </TabBtn>
            <TabBtn id="parts" active={activeTab} onClick={setActiveTab}>
              {t('printerSettings.tab.parts')}
            </TabBtn>
          </div>
        </div>

        <div className="p-4">
          {isLoading || !data ? (
            <div className="space-y-3">
              {[0, 1, 2, 3].map((i) => (
                <div key={i} className="animate-pulse h-10 bg-bambu-dark rounded" />
              ))}
            </div>
          ) : activeTab === 'print_options' ? (
            <PrintOptionsTab data={data} onSubmit={onSubmit} />
          ) : (
            <PrinterPartsTab data={data} onRefetch={() => refetch()} />
          )}
        </div>
      </div>
    </div>
  );
}

function TabBtn({
  id, active, onClick, children,
}: { id: TabId; active: TabId; onClick: (id: TabId) => void; children: React.ReactNode }) {
  const isActive = id === active;
  return (
    <button
      type="button"
      onClick={() => onClick(id)}
      className={`px-3 py-1.5 text-sm rounded-md transition-colors ${
        isActive ? 'bg-bambu-green text-white' : 'text-bambu-gray hover:text-white'
      }`}
    >
      {children}
    </button>
  );
}
```

- [ ] **Step 4: Run tests — confirm pass**

Run: `cd frontend && npm run test:run -- PrinterSettingsModal`
Expected: 4 PASSED.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/PrinterSettingsModal.tsx \
        frontend/src/__tests__/components/PrinterSettingsModal.test.tsx
git commit -m "feat(frontend): PrinterSettingsModal with two tabs"
```

---

## Task 15: Wire kebab menu item in PrintersPage

**Files:**
- Modify: `frontend/src/pages/PrintersPage.tsx`

- [ ] **Step 1: Add state + import**

Near the existing `import { AMSSettingsModal } from '../components/AMSSettingsModal';` line, append:

```typescript
import { PrinterSettingsModal } from '../components/PrinterSettingsModal';
```

Locate the `const [amsSettingsOpen, setAmsSettingsOpen] = useState(false);` line (added during AMS Settings work). Directly after it, append:

```typescript
const [printerSettingsOpen, setPrinterSettingsOpen] = useState(false);
```

- [ ] **Step 2: Add menu button to the kebab dropdown**

Find the kebab dropdown (search `t('printers.calibration.menuItem')`). After the **Calibration** menu button's closing `</button>`, add:

```tsx
                  <button
                    className={`w-full px-4 py-2 text-left text-sm hover:bg-bambu-dark-tertiary flex items-center gap-2 ${
                      !hasPermission('printers:update') ? 'opacity-50 cursor-not-allowed' : ''
                    }`}
                    onClick={() => {
                      if (!hasPermission('printers:update')) return;
                      setPrinterSettingsOpen(true);
                      setShowMenu(false);
                    }}
                  >
                    <Settings className="w-4 h-4" />
                    {t('printerSettings.menuItem')}
                  </button>
```

(`Settings` icon is already imported from `lucide-react` — it was added during the AMS Settings work for the gear icon on the AMS panel.)

- [ ] **Step 3: Mount the modal**

Find the existing `<AMSSettingsModal />` mount (search `AMSSettingsModal isOpen`). Directly after that block, append:

```tsx
      <PrinterSettingsModal
        isOpen={printerSettingsOpen}
        onClose={() => setPrinterSettingsOpen(false)}
        printerId={printer.id}
      />
```

- [ ] **Step 4: tsc + lint smoke**

Run: `cd frontend && npx tsc --noEmit && npm run lint`
Expected: clean (or only pre-existing warnings).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/PrintersPage.tsx
git commit -m "feat(frontend): kebab menu item + PrinterSettingsModal mount"
```

---

## Task 16: Build + CHANGELOG + full verify pass

**Files:**
- Modify: `frontend/static/*` (generated)
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Frontend build**

Run: `cd frontend && npm run build`
Expected: clean.

- [ ] **Step 2: CHANGELOG entry (concise, per project rule)**

Open `CHANGELOG.md`. Under the existing `## [Unreleased]` section's `### Added` list (right after the AMS Settings entry), append:

```markdown
- **Printer Settings dialog.** New kebab-menu item on each printer card opens a tabbed modal (Print Options + Printer Parts) — Bambu Studio parity for ~15 toggles (AI detections with sensitivity, filament tangle, nozzle blob, FOD check, open-door check, purify air, auto recovery, prompt sound, snapshot, build-plate detection). Read-only nozzle info on Parts tab. All gated by `printers:update`; per-model `supports.*` hides unsupported rows. Audit table `printer_setting_audit` (m061) records every applied change.
```

- [ ] **Step 3: Run full backend test suite for the new feature**

```bash
cd D:/Development/bamdude
ruff check backend/
pytest backend/tests/unit/services/test_bambu_mqtt_printer_settings.py \
       backend/tests/unit/services/test_printer_capabilities.py \
       backend/tests/integration/test_m061_printer_setting_audit_migration.py \
       backend/tests/integration/test_printer_settings_routes.py -v
```

Expected: ruff clean, all 4 files pass.

- [ ] **Step 4: Frontend tests + lint**

```bash
cd frontend && npm run lint && npx tsc --noEmit && npm run test:run -- PrinterSettingsModal
```

Expected: clean.

- [ ] **Step 5: Commit bundle + CHANGELOG**

```bash
git add CHANGELOG.md frontend/static
git commit -m "chore(release): Printer Settings dialog bundle + CHANGELOG"
```

- [ ] **Step 6: Manual hardware check (documented for the user)**

The implementation is complete. Real-hardware verification step:

1. Start backend with `DEBUG=true uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000` and frontend dev server pointing to it.
2. Open the printer page → kebab menu → "Printer Settings".
3. Confirm Print Options tab renders only rows your printer's `supports.*` allows (e.g. P1S sees only Behaviour group + maybe door check; X1C/H2D see AI detections + sensors).
4. Toggle **Auto recovery** OFF → MQTT log shows `print_option` with `auto_recovery: false`. Bambu Studio sees the same change within 5 s.
5. Pick a sensitivity (Low/Medium/High) for **Spaghetti detection** on X1C → MQTT shows `xcam_control_set` with `module_name=spaghetti_detector` and `halt_print_sensitivity=<level>`.
6. Switch to **Printer Parts** tab → nozzle info displayed (read-only). Refresh button re-fetches.
7. Check `SELECT * FROM printer_setting_audit ORDER BY id DESC LIMIT 10;` — one row per applied click, all `result='sent'`.

Don't mark this step done until at least 3 of the toggles round-trip through a real printer.

---

## Self-Review

**Spec coverage check (each §):**
- §1 Goal → Tasks 8-15 collectively deliver the modal. ✓
- §2 Authoritative source — handled inline by task comments referencing BS files. ✓
- §3 Out of scope — `set_nozzle` returns 409 in Task 9, no real publisher. ✓
- §4 Architecture → matches Task 1-9 layering. ✓
- §5 Actions and MQTT — Tasks 4 (print_option bool/int), 5 (xcam wrapper), Camera snapshot covered in Task 4. ✓
- §6 State reads — Task 3 (PrintOptions extension), Task 6 (parser echoes). ✓
- §7 Backend → Tasks 1-2 (migration+model), 7 (capabilities), 8 (schemas), 9 (router). ✓
- §8 Frontend → Tasks 10-15. ✓
- §9 i18n → Task 11. ✓
- §10 Errors/edge → covered in Task 9 tests. ✓
- §11 Open questions — set_nozzle deferred (§11.2 handled by 409 stub); stop-calibration (§11.1) not implemented in this plan (Calibration kept separate per user's decision); xcam.cfg bits 17+ (§11.3) — out of scope since we use direct print_option echoes; open_door 3-state (§11.4) — handled via int 0/1/2 in Task 12 SegmentedRow. ✓
- §12 Tests → Tasks 1, 4-7, 9, 14 + Task 16 manual. ✓
- §13 Rollout → Task 16. ✓

**Placeholder scan:**
- "(verify each line against the .po before committing — if it has a real translation, use it)" in Task 11 step 3 — instruction, not placeholder. Acceptable.
- No "TBD", "TODO", "implement later" anywhere in tasks. ✓

**Type consistency:**
- `PrinterSettingsPostBody` action discriminators match across schemas (Task 8), router (Task 9), client.ts types (Task 10), hook flagsForAction (Task 10), modal usage (Task 14). ✓
- Publisher method names (`print_option_auto_recovery`, …, `xcam_control_for_settings`, `camera_snapshot_enable`) match in Task 4-5 (defined) and Task 9 (router calls). ✓
- `supports.*` keys match across helper (Task 7), schemas (Task 8), router gate map (Task 9), tab visibility checks (Task 12-13). ✓
- Permission `PRINTERS_UPDATE` consistent in Task 9 + Task 15 kebab gate. ✓

No gaps found.
