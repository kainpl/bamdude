# AMS Settings Dialog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port Bambu Studio's *AMS Settings* dialog into BamDude — gear-icon modal on the printer page that lets operators toggle 4 AMS-level flags (insertion/power-on RFID read, remaining-capacity estimate, filament backup) plus air-print detection, calibrate AMS, switch AMS firmware (A1 LITE↔FULL), and rearrange AMS unit IDs.

**Architecture:** Thin pass-through to MQTT. Printer is source of truth — we mirror state from `print.ams.*` and `cfg` bitfield in push messages into `PrinterState`, expose it over a new REST router, and publish commands back via existing `bambu_mqtt.py` plumbing. One new migration adds an `ams_setting_audit` table (forensic trail for `PRINTERS_UPDATE`-gated changes — BS has no RBAC and we do). Gear icon lives on existing AMS panel in `frontend/src/pages/PrintersPage.tsx`; new `AMSSettingsModal.tsx` mirrors `ConfigureAmsSlotModal.tsx` style.

**Tech Stack:** Python 3.10 / FastAPI / SQLAlchemy 2.0 async / pytest / aiomqtt · React 19 / TypeScript / Tailwind 4 / TanStack Query 5 / vitest.

**Spec:** `docs/superpowers/specs/2026-05-12-ams-settings-dialog-design.md`

---

## Open decisions resolved at plan-time

1. **Permission:** Spec said `PRINTERS_MANAGE`; that enum doesn't exist. We use `Permission.PRINTERS_UPDATE` (already present, semantically closest — "manage printer config", more restrictive than `PRINTERS_CONTROL` which is start/stop). If the user wants a dedicated `PRINTERS_AMS_SETTINGS` permission, that's a follow-up — out of scope here.
2. **Migration number:** Latest is `m059_stock_forecasting.py`. New migration is **`m060_ams_setting_audit.py`** (version=60).
3. **firmware_switch / reorder MQTT payloads:** BS source for `CrtlSwitchFirmware` and `OnBtnRearrangeClicked` is non-trivial. Research is **Task 6** before implementing those two methods. If research stalls, those two surfaces stay behind `supports.firmware_switch=false` / `supports.reorder=false` and ship in a phase-2.

---

## File Structure

**New files**

- `backend/app/migrations/m060_ams_setting_audit.py` — DDL for audit table.
- `backend/app/models/ams_setting_audit.py` — SQLAlchemy model.
- `backend/app/schemas/ams_settings.py` — Pydantic request/response schemas (state, supports, 6 action bodies, discriminated union).
- `backend/app/api/routes/ams_settings.py` — `GET` + `POST` router.
- `backend/app/services/ams_capabilities.py` — small helper that derives `supports.*` from `PrinterState` + printer model code. Isolated so the lookup table is easy to extend and unit-test.
- `backend/tests/unit/services/test_bambu_mqtt_ams_settings.py` — unit tests for the 6 new MQTT publishers + push-parser additions + hold-timer.
- `backend/tests/unit/services/test_ams_capabilities.py` — unit tests for the gating helper.
- `backend/tests/integration/api/test_ams_settings_routes.py` — integration tests for the new router.
- `backend/tests/unit/migrations/test_m060_ams_setting_audit.py` — migration smoke test.
- `frontend/src/components/AMSSettingsModal.tsx` — the dialog.
- `frontend/src/hooks/useAmsSettings.ts` — TanStack Query hook + WS-invalidation wiring + client-side hold-timer state.
- `frontend/src/__tests__/components/AMSSettingsModal.test.tsx` — component tests.

**Modified files**

- `backend/app/services/bambu_mqtt.py` — add 6 publisher methods, extend `PrinterState` dataclass with `ams_system_setting` dict, extend the `print.*` push parser, add hold-timer state.
- `backend/app/core/database.py` — register new `AmsSettingAudit` model so `create_all` picks it up on fresh installs.
- `backend/app/main.py` — register the new router.
- `frontend/src/api/client.ts` — add 2 new typed methods (`getAmsSettings`, `postAmsSettings`).
- `frontend/src/pages/PrintersPage.tsx` — add gear icon + `<AMSSettingsModal />` instance.
- `frontend/src/i18n/locales/en.ts` — add `amsSettings` block (English strings from BS `_L()`).
- `frontend/src/i18n/locales/uk.ts` — add `amsSettings` block (Ukrainian strings from `BambuStudio_uk.po`; flag empty msgstr inline).

---

## Test Strategy

- TDD where natural: each new MQTT publisher and parser branch gets a unit test before implementation. Routes get integration tests that assert MQTT publish was called + audit row written.
- Migration gets a smoke test that runs it against an in-memory SQLite, asserts the table+index exist, and that re-running it is a no-op (idempotency).
- Frontend modal gets vitest component tests for: skeleton state, hidden rows for unsupported flags, optimistic toggle, error revert, confirm-dialog before destructive actions.
- **No end-to-end test against a real printer in this plan** — virtual_printer fixtures cover what we can simulate; real-hardware verification is documented as a manual step at the end.

---

## Task 1: Migration `m060_ams_setting_audit`

**Files:**
- Create: `backend/app/migrations/m060_ams_setting_audit.py`
- Create: `backend/tests/unit/migrations/test_m060_ams_setting_audit.py`

- [ ] **Step 1: Write the migration smoke test**

```python
# backend/tests/unit/migrations/test_m060_ams_setting_audit.py
"""Smoke test for m060 — ams_setting_audit table + index."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m060_ams_setting_audit


@pytest.mark.asyncio
async def test_m060_creates_table_and_index():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        # Minimal prerequisites: printers + users tables (FK targets).
        await conn.execute(text(
            "CREATE TABLE printers (id INTEGER PRIMARY KEY, name TEXT)"
        ))
        await conn.execute(text(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)"
        ))
        await m060_ams_setting_audit.upgrade(conn)

        rows = (await conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ams_setting_audit'"
        ))).fetchall()
        assert len(rows) == 1

        indexes = (await conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='ix_ams_setting_audit_printer'"
        ))).fetchall()
        assert len(indexes) == 1


@pytest.mark.asyncio
async def test_m060_is_idempotent():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE printers (id INTEGER PRIMARY KEY, name TEXT)"
        ))
        await conn.execute(text(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)"
        ))
        await m060_ams_setting_audit.upgrade(conn)
        # Second run must not raise.
        await m060_ams_setting_audit.upgrade(conn)
```

- [ ] **Step 2: Run test — confirm it fails (module missing)**

Run: `pytest backend/tests/unit/migrations/test_m060_ams_setting_audit.py -v`
Expected: `ImportError` or collection error — `m060_ams_setting_audit` doesn't exist yet.

- [ ] **Step 3: Write the migration**

```python
# backend/app/migrations/m060_ams_setting_audit.py
"""Audit trail for AMS settings dialog (BamDude port of Bambu Studio AMSSetting).

Why this exists:
    BambuStudio's AMS Settings dialog has no audit trail and no RBAC — anyone
    with the desktop slicer can flip ``calibrate_remain_flag`` or
    ``auto_switch_filament`` on every printer they're paired with. On a farm
    we gate the same surface behind ``Permission.PRINTERS_UPDATE`` and record
    one row per applied change so operators can answer "who turned RFID
    auto-read off?" without diffing MQTT logs.

What this migration does:
    Creates ``ams_setting_audit`` with (printer_id, user_id, action,
    payload_json, sequence_id, result, error_message, created_at) plus a
    descending index on ``(printer_id, created_at DESC)`` for the most-recent-
    first lookup the future UI will want.

Idempotent: ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS``.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import table_exists

version = 60
name = "ams_setting_audit"


async def upgrade(conn):
    if not await table_exists(conn, "ams_setting_audit"):
        if is_postgres():
            await conn.execute(text(
                """
                CREATE TABLE ams_setting_audit (
                    id SERIAL PRIMARY KEY,
                    printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    action VARCHAR(50) NOT NULL,
                    payload_json TEXT NOT NULL,
                    sequence_id VARCHAR(50),
                    result VARCHAR(20) NOT NULL,
                    error_message TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT now()
                )
                """
            ))
        else:
            await conn.execute(text(
                """
                CREATE TABLE ams_setting_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    sequence_id TEXT,
                    result TEXT NOT NULL,
                    error_message TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT (CURRENT_TIMESTAMP)
                )
                """
            ))

    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_ams_setting_audit_printer "
        "ON ams_setting_audit(printer_id, created_at DESC)"
    ))
```

- [ ] **Step 4: Run tests — confirm both pass**

Run: `pytest backend/tests/unit/migrations/test_m060_ams_setting_audit.py -v`
Expected: 2 PASSED.

- [ ] **Step 5: Commit**

```bash
git add backend/app/migrations/m060_ams_setting_audit.py \
        backend/tests/unit/migrations/test_m060_ams_setting_audit.py
git commit -m "feat(migrations): m060 ams_setting_audit table for AMS settings forensic trail"
```

---

## Task 2: `AmsSettingAudit` model

**Files:**
- Create: `backend/app/models/ams_setting_audit.py`
- Modify: `backend/app/core/database.py` (one import line near the other `from backend.app.models.*` imports)

- [ ] **Step 1: Add the model**

```python
# backend/app/models/ams_setting_audit.py
"""Audit row for one applied AMS-settings change.

Written by ``backend/app/api/routes/ams_settings.py`` after each successful
MQTT publish (or after a publish error — ``result`` discriminates). Read by
nobody yet; surfaced in a future viewer UI.
"""

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class AmsSettingAudit(Base):
    __tablename__ = "ams_setting_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(
        ForeignKey("printers.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    sequence_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    result: Mapped[str] = mapped_column(String(20), nullable=False)  # 'sent' | 'error'
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_ams_setting_audit_printer", "printer_id", "created_at"),
    )
```

- [ ] **Step 2: Register in database.py**

Open `backend/app/core/database.py`. Locate the block where models are imported for `Base.metadata`. Add **one line** in alphabetical position with the other `ams_*` imports:

```python
from backend.app.models.ams_setting_audit import AmsSettingAudit  # noqa: F401
```

- [ ] **Step 3: Verify with a quick import smoke**

Run: `python -c "from backend.app.models.ams_setting_audit import AmsSettingAudit; print(AmsSettingAudit.__tablename__)"`
Expected: `ams_setting_audit`

- [ ] **Step 4: Verify Base.metadata picks it up**

Run: `python -c "from backend.app.core.database import Base; print('ams_setting_audit' in Base.metadata.tables)"`
Expected: `True`

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/ams_setting_audit.py backend/app/core/database.py
git commit -m "feat(models): AmsSettingAudit + register in database.py"
```

---

## Task 3: Extend `PrinterState` with `ams_system_setting`

**Files:**
- Modify: `backend/app/services/bambu_mqtt.py:137-220` (PrinterState dataclass)

We only add fields here — no logic. Parser changes are Task 5; publishers are Task 4.

- [ ] **Step 1: Add fields to `PrinterState`**

Find `@dataclass class PrinterState:` (line 137-ish). After the existing `ams_extruder_map` field (~line 209), add:

```python
    # ---------- AMS system-level user settings (BS "AMS Settings" dialog) ----------
    # Each flag mirrors the corresponding push field from print.ams (insert_flag,
    # power_on_flag, calibrate_remain_flag) and the cfg bitfield (auto_switch
    # bit 10 X1 / bit 18 P1+A1, air-print support bit 29). None means "printer
    # hasn't reported it yet" — distinct from False ("printer says off").
    ams_insertion_update: bool | None = None
    ams_power_on_update: bool | None = None
    ams_remain_capacity: bool | None = None
    ams_auto_switch_filament: bool | None = None
    ams_air_print_detect: bool | None = None
    ams_firmware_idx_run: int | None = None
    ams_firmware_idx_sel: int | None = None
    # Hold-timer: when we publish an AMS setting command we stamp the flag
    # name here; the push parser skips overwriting the corresponding field
    # while ``time.time() - hold < 3.0``. Avoids the toggle visually flipping
    # back during the half-second printer-confirms-the-change round-trip.
    ams_settings_hold: dict = field(default_factory=dict)  # flag_name -> epoch_seconds
```

- [ ] **Step 2: Smoke-check the dataclass still imports**

Run: `python -c "from backend.app.services.bambu_mqtt import PrinterState; s = PrinterState(); print(s.ams_insertion_update, s.ams_settings_hold)"`
Expected: `None {}`

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/bambu_mqtt.py
git commit -m "feat(mqtt): PrinterState fields for AMS system settings + hold-timer"
```

---

## Task 4: 4 MQTT publisher methods (the simple four)

We tackle the four whose payloads come directly from BS DeviceManager.cpp (`ams_user_settings`, `print_option_auto_switch_filament`, `print_option_air_print_detect`, `ams_calibrate`). Firmware-switch and reorder are research-gated and split into Task 6.

**Files:**
- Modify: `backend/app/services/bambu_mqtt.py` — add four async methods near the existing `ams_*` block (after `reset_ams_slot`, ~line 4760).
- Create: `backend/tests/unit/services/test_bambu_mqtt_ams_settings.py`

- [ ] **Step 1: Write the failing payload tests**

```python
# backend/tests/unit/services/test_bambu_mqtt_ams_settings.py
"""Unit tests for new AMS-settings MQTT publishers + push parsing."""

import json
import time
from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient, PrinterState


def _make_client_with_capture():
    """Return (client, captured_payloads). Client publish replaced with capture."""
    client = BambuMQTTClient.__new__(BambuMQTTClient)
    client.state = PrinterState()
    client.printer_serial = "TEST00000000"
    client.printer_id = 1
    captured: list[dict] = []

    def fake_publish(payload, qos=0):
        captured.append(json.loads(payload) if isinstance(payload, str) else payload)
        return True

    client._publish = MagicMock(side_effect=fake_publish)
    return client, captured


def test_ams_user_setting_payload_shape():
    client, captured = _make_client_with_capture()
    seq = client.ams_user_setting(startup_read=True, tray_read=False, calibrate_remain=True)

    assert seq is not None
    assert len(captured) == 1
    msg = captured[0]
    assert msg["print"]["command"] == "ams_user_setting"
    assert msg["print"]["ams_id"] == -1
    assert msg["print"]["startup_read_option"] is True
    assert msg["print"]["tray_read_option"] is False
    assert msg["print"]["calibrate_remain_flag"] is True
    assert msg["print"]["sequence_id"] == seq


def test_ams_user_setting_stamps_hold_timer():
    client, _ = _make_client_with_capture()
    before = time.time()
    client.ams_user_setting(startup_read=False, tray_read=False, calibrate_remain=False)
    after = time.time()
    for flag in ("ams_insertion_update", "ams_power_on_update", "ams_remain_capacity"):
        ts = client.state.ams_settings_hold.get(flag)
        assert ts is not None
        assert before <= ts <= after


def test_print_option_auto_switch_filament_payload():
    client, captured = _make_client_with_capture()
    client.print_option_auto_switch_filament(enabled=True)
    msg = captured[0]
    assert msg["print"]["command"] == "print_option"
    assert msg["print"]["auto_switch_filament"] is True
    assert "sequence_id" in msg["print"]


def test_print_option_air_print_detect_payload():
    client, captured = _make_client_with_capture()
    client.print_option_air_print_detect(enabled=False)
    msg = captured[0]
    assert msg["print"]["command"] == "print_option"
    assert msg["print"]["air_print_detect"] is False


def test_ams_calibrate_emits_gcode():
    client, captured = _make_client_with_capture()
    # ams_calibrate uses gcode_line, not direct JSON command. Patch the gcode helper.
    client._publish_gcode = MagicMock(return_value=True)
    client.ams_calibrate(ams_id=2)
    client._publish_gcode.assert_called_once()
    assert "M620 C2" in client._publish_gcode.call_args.args[0]
```

- [ ] **Step 2: Run tests — confirm they fail (methods missing)**

Run: `pytest backend/tests/unit/services/test_bambu_mqtt_ams_settings.py -v`
Expected: `AttributeError: 'BambuMQTTClient' object has no attribute 'ams_user_setting'` or similar.

- [ ] **Step 3: Inspect the existing publisher style**

Read `backend/app/services/bambu_mqtt.py` around `ams_set_filament_setting` (rough line 4661). Note:
- How `sequence_id` is generated (look for `self._next_sequence_id()` or `_sequence_id` increment).
- How JSON is published (`self._publish(json.dumps(...))` or similar).
- The exact signature pattern for sync vs async methods in this class.

This is **read-only** — don't edit yet. Confirm the helper names; the test stubs assume `_publish` and `_publish_gcode`. Rename in the test if the real method names differ.

- [ ] **Step 4: Add the four publishers**

After the last `ams_*` method in `bambu_mqtt.py` (search `def reset_ams_slot` and append after its closing), add:

```python
    def ams_user_setting(
        self,
        startup_read: bool,
        tray_read: bool,
        calibrate_remain: bool,
    ) -> str | None:
        """BS ``command_ams_user_settings`` (DeviceManager.cpp:1575).

        Sends the three RFID/remain toggles in one message. ``ams_id=-1`` is
        BS's "apply to every AMS on this printer" convention. Sets hold-timers
        on all three corresponding state fields so the push parser doesn't
        clobber the just-sent value while the printer confirms.
        """
        seq = self._next_sequence_id()
        payload = {
            "print": {
                "command": "ams_user_setting",
                "sequence_id": seq,
                "ams_id": -1,
                "startup_read_option": bool(startup_read),
                "tray_read_option": bool(tray_read),
                "calibrate_remain_flag": bool(calibrate_remain),
            }
        }
        if not self._publish(json.dumps(payload)):
            return None
        now = time.time()
        self.state.ams_settings_hold["ams_insertion_update"] = now
        self.state.ams_settings_hold["ams_power_on_update"] = now
        self.state.ams_settings_hold["ams_remain_capacity"] = now
        return seq

    def print_option_auto_switch_filament(self, enabled: bool) -> str | None:
        """BS ``command_ams_switch_filament`` (DeviceManager.cpp:1751).

        Note: BS routes this through ``print.command = "print_option"``, not
        ``ams_user_setting``. Same go for ``air_print_detect``.
        """
        seq = self._next_sequence_id()
        payload = {
            "print": {
                "command": "print_option",
                "sequence_id": seq,
                "auto_switch_filament": bool(enabled),
            }
        }
        if not self._publish(json.dumps(payload)):
            return None
        self.state.ams_settings_hold["ams_auto_switch_filament"] = time.time()
        return seq

    def print_option_air_print_detect(self, enabled: bool) -> str | None:
        """BS ``command_ams_air_print_detect`` (DeviceManager.cpp:1765)."""
        seq = self._next_sequence_id()
        payload = {
            "print": {
                "command": "print_option",
                "sequence_id": seq,
                "air_print_detect": bool(enabled),
            }
        }
        if not self._publish(json.dumps(payload)):
            return None
        self.state.ams_settings_hold["ams_air_print_detect"] = time.time()
        return seq

    def ams_calibrate(self, ams_id: int) -> bool:
        """BS ``command_ams_calibrate`` (DeviceManager.cpp:1595): ``M620 C<id>``.

        Wrapped via the existing gcode-line publisher so the printer's
        ``gcode_claim_action`` envelope is added consistently with our other
        macro calls.
        """
        gcode = f"M620 C{int(ams_id)}\n"
        return bool(self._publish_gcode(gcode))
```

If the surrounding methods are `async def` (check Task 4 step 3), make these `async def` as well and `await self._publish(...)`. Adjust the tests accordingly (mark them `@pytest.mark.asyncio` and `await` the calls).

- [ ] **Step 5: Re-run the tests — confirm all pass**

Run: `pytest backend/tests/unit/services/test_bambu_mqtt_ams_settings.py -v`
Expected: 5 PASSED.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/bambu_mqtt.py \
        backend/tests/unit/services/test_bambu_mqtt_ams_settings.py
git commit -m "feat(mqtt): ams_user_setting + print_option (switch/air-print) + ams_calibrate"
```

---

## Task 5: Push-parser additions — read insert_flag, power_on_flag, calibrate_remain_flag, cfg bits

The MQTT parser currently doesn't surface the AMS system flags. We add them, honoring the hold-timer.

**Files:**
- Modify: `backend/app/services/bambu_mqtt.py` — find `_handle_ams_data` (~line 1411) and the `cfg`-parsing block (search `home_flag` or `stat`). Add four blocks.
- Modify: `backend/tests/unit/services/test_bambu_mqtt_ams_settings.py` — append parser tests.

- [ ] **Step 1: Append parser tests**

```python
# Add to backend/tests/unit/services/test_bambu_mqtt_ams_settings.py

def _state_after_push(client: BambuMQTTClient, push_msg: dict):
    """Drive the parser the same way the MQTT layer does."""
    # Use whichever entrypoint the existing parser exposes. Most likely
    # client._handle_print_data(push_msg["print"]) or similar — confirm
    # via grep in step below.
    client._handle_print_data(push_msg["print"])
    return client.state


def test_parser_reads_ams_insert_flag():
    client, _ = _make_client_with_capture()
    msg = {"print": {"ams": {"insert_flag": True, "power_on_flag": False,
                              "calibrate_remain_flag": True}}}
    state = _state_after_push(client, msg)
    assert state.ams_insertion_update is True
    assert state.ams_power_on_update is False
    assert state.ams_remain_capacity is True


def test_parser_respects_hold_timer():
    client, _ = _make_client_with_capture()
    client.state.ams_insertion_update = False
    # Stamp a hold that's still active.
    client.state.ams_settings_hold["ams_insertion_update"] = time.time()

    msg = {"print": {"ams": {"insert_flag": True}}}
    state = _state_after_push(client, msg)
    # Push value (True) was ignored; previous local value (False) preserved.
    assert state.ams_insertion_update is False


def test_parser_releases_hold_after_3s():
    client, _ = _make_client_with_capture()
    client.state.ams_insertion_update = False
    # Stamp a hold 5 seconds ago — should be released.
    client.state.ams_settings_hold["ams_insertion_update"] = time.time() - 5.0

    msg = {"print": {"ams": {"insert_flag": True}}}
    state = _state_after_push(client, msg)
    assert state.ams_insertion_update is True


def test_parser_reads_auto_switch_filament_p1a1_bit18():
    client, _ = _make_client_with_capture()
    # bit 18 set → True. Use printer_model that picks bit 18 (P1 family).
    client.printer_model = "C11"  # P1S — bit 18 per BS
    msg = {"print": {"cfg": (1 << 18)}}
    state = _state_after_push(client, msg)
    assert state.ams_auto_switch_filament is True


def test_parser_reads_auto_switch_filament_x1_bit10():
    client, _ = _make_client_with_capture()
    client.printer_model = "BL-P001"  # X1C — bit 10 per BS
    msg = {"print": {"cfg": (1 << 10)}}
    state = _state_after_push(client, msg)
    assert state.ams_auto_switch_filament is True


def test_parser_reads_air_print_detect_field():
    client, _ = _make_client_with_capture()
    msg = {"print": {"air_print_detect": True}}
    state = _state_after_push(client, msg)
    assert state.ams_air_print_detect is True
```

- [ ] **Step 2: Confirm parser entrypoint name**

Run: `grep -n "def _handle_ams_data\|def _handle_print_data\|def _process_message" backend/app/services/bambu_mqtt.py`

Pick the entrypoint that processes the `print` dict from the push. Adjust `_state_after_push` in the test to call the right method. If parsing is split across multiple helpers, drive the test through the topmost one and assert against `client.state` after.

- [ ] **Step 3: Run tests — confirm they fail**

Run: `pytest backend/tests/unit/services/test_bambu_mqtt_ams_settings.py -v -k "parser"`
Expected: 6 FAILED (fields not populated).

- [ ] **Step 4: Add parser logic**

Inside the existing `print.ams` parsing block (`_handle_ams_data` or equivalent), add **after** the existing ams_status / temperature / humidity reads:

```python
        # AMS system-level user settings (BS push fields).
        # Each respects the 3-second hold-timer so just-sent commands aren't
        # clobbered by the printer's interleaved status echo.
        _now = time.time()
        _hold_ttl = 3.0

        def _hold_active(flag: str) -> bool:
            ts = self.state.ams_settings_hold.get(flag)
            return ts is not None and (_now - ts) < _hold_ttl

        if "insert_flag" in ams_data and not _hold_active("ams_insertion_update"):
            self.state.ams_insertion_update = bool(ams_data["insert_flag"])
        if "power_on_flag" in ams_data and not _hold_active("ams_power_on_update"):
            self.state.ams_power_on_update = bool(ams_data["power_on_flag"])
        if "calibrate_remain_flag" in ams_data and not _hold_active("ams_remain_capacity"):
            self.state.ams_remain_capacity = bool(ams_data["calibrate_remain_flag"])
```

Where `ams_data` is the dict already extracted from `print["ams"]`. Adjust variable name to match local code.

Find the existing `cfg`-bits parsing (`grep -n "cfg" backend/app/services/bambu_mqtt.py` — look for `home_flag` or `stat`-style integer parses). Add, alongside other bit-reads:

```python
        # AMS auto-switch-filament (filament backup) — bit position depends on
        # printer family. BS DeviceManager.cpp:1032 uses bit 10 for X1, bit 18
        # for P1 / A1. Choose based on printer_model code; default to bit 18 if
        # model is unknown (P1/A1 are the more common family).
        if cfg is not None and not _hold_active("ams_auto_switch_filament"):
            x1_models = {"BL-P001", "BL-P002", "BL-P003", "BL-P004",
                         "BL-P005", "C13", "N2S"}  # X1C, X1E, X1, X2D
            bit = 10 if (self.printer_model in x1_models) else 18
            self.state.ams_auto_switch_filament = bool((cfg >> bit) & 0x1)

        # Air-print detect — direct field in push echo of print_option command.
        if "air_print_detect" in print_data and not _hold_active("ams_air_print_detect"):
            self.state.ams_air_print_detect = bool(print_data["air_print_detect"])
```

Adjust `cfg` variable name to whatever the surrounding code already binds (`cfg = print_data.get("cfg")` is most likely). Adjust `self.printer_model` to the actual attribute holding the model code (`grep -n "self.printer_model\|self.model_code" backend/app/services/bambu_mqtt.py`).

- [ ] **Step 5: Re-run all bambu_mqtt_ams_settings tests**

Run: `pytest backend/tests/unit/services/test_bambu_mqtt_ams_settings.py -v`
Expected: 11 PASSED (5 from Task 4 + 6 from this task).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/bambu_mqtt.py \
        backend/tests/unit/services/test_bambu_mqtt_ams_settings.py
git commit -m "feat(mqtt): parse ams.insert/power_on/remain flags + cfg backup bit + air_print echo"
```

---

## Task 6: Research firmware_switch + reorder MQTT payloads

This task is research, not code. Output: a one-page note inside the spec's `## 11. Open questions` section listing the exact payload shape (or "still unknown, deferring") so Tasks 7+10 can decide whether to ship those features now.

**Files:**
- Modify: `docs/superpowers/specs/2026-05-12-ams-settings-dialog-design.md` (replace TBD bullets with findings)

- [ ] **Step 1: Read BS firmware-switch implementation**

```bash
grep -rn "CrtlSwitchFirmware\|ams_firmware_switch\|GetCurrentFirmwareIdxRun" temp/references/BambuStudio/src
```

Open the file containing `CrtlSwitchFirmware` (likely `src/slic3r/GUI/DeviceCore/DevFilaAmsSettingCtrl.cpp`) and read the body — note the command name (probably `print.command = "..."`), and the field names.

- [ ] **Step 2: Read BS reorder implementation**

```bash
grep -rn "ams_reorder\|command_ams_reorder\|OnBtnRearrangeClicked" temp/references/BambuStudio/src
```

Open `AMSSettingArrangeAMSOrder::OnBtnRearrangeClicked` body and trace which MachineObject method it calls.

- [ ] **Step 3: Write findings into the spec**

Edit `docs/superpowers/specs/2026-05-12-ams-settings-dialog-design.md` section 11 — replace the "Payload for `ams_firmware_switch`" and "Payload for `ams_reorder`" bullets with the actual payload shape (one block each), OR mark the feature as `[DEFERRED to phase 2]` with a one-sentence reason and follow-up issue link.

- [ ] **Step 4: Commit (docs-only)**

```bash
git add docs/superpowers/specs/2026-05-12-ams-settings-dialog-design.md
git commit -m "docs(spec): resolve ams_firmware_switch / ams_reorder payload research"
```

---

## Task 7: `ams_firmware_switch` + `ams_reorder` MQTT publishers

**Conditional:** Execute only if Task 6 resolved the payloads. If deferred, skip this task — the supports flags will already be False so no UI surface is exposed.

**Files:**
- Modify: `backend/app/services/bambu_mqtt.py` — two more methods after the four from Task 4.
- Modify: `backend/tests/unit/services/test_bambu_mqtt_ams_settings.py` — two more tests.

- [ ] **Step 1: Write tests for the two new publishers**

```python
# Append to test_bambu_mqtt_ams_settings.py

def test_ams_firmware_switch_payload_shape():
    client, captured = _make_client_with_capture()
    seq = client.ams_firmware_switch(firmware_idx=1)
    msg = captured[0]
    # Replace assertions below with the exact shape resolved in Task 6.
    assert msg["print"]["command"] == "<command_from_task6>"
    assert msg["print"]["firmware_idx"] == 1
    assert seq is not None


def test_ams_reorder_payload_shape():
    client, captured = _make_client_with_capture()
    seq = client.ams_reorder(order=[0, 1, 2, 3])
    msg = captured[0]
    # Replace assertions with the exact shape resolved in Task 6.
    assert msg["print"]["command"] == "<command_from_task6>"
    assert msg["print"]["<order_field>"] == [0, 1, 2, 3]
```

- [ ] **Step 2: Run tests — confirm they fail**

Run: `pytest backend/tests/unit/services/test_bambu_mqtt_ams_settings.py::test_ams_firmware_switch_payload_shape backend/tests/unit/services/test_bambu_mqtt_ams_settings.py::test_ams_reorder_payload_shape -v`
Expected: FAILED, AttributeError.

- [ ] **Step 3: Add publishers**

After `ams_calibrate` in bambu_mqtt.py, add `ams_firmware_switch(firmware_idx: int) -> str | None` and `ams_reorder(order: list[int]) -> str | None`. Use the payload resolved in Task 6.

- [ ] **Step 4: Run tests — confirm pass**

Run: `pytest backend/tests/unit/services/test_bambu_mqtt_ams_settings.py -v`
Expected: 13 PASSED.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/bambu_mqtt.py \
        backend/tests/unit/services/test_bambu_mqtt_ams_settings.py
git commit -m "feat(mqtt): ams_firmware_switch + ams_reorder publishers"
```

---

## Task 8: Capabilities helper `ams_capabilities.py`

Builds the `supports.*` dict from `PrinterState` + model code. Isolated for easy unit-testing and future extension.

**Files:**
- Create: `backend/app/services/ams_capabilities.py`
- Create: `backend/tests/unit/services/test_ams_capabilities.py`

- [ ] **Step 1: Write the gating tests**

```python
# backend/tests/unit/services/test_ams_capabilities.py
"""Tests for compute_ams_supports — drives UI row visibility."""

from backend.app.services.ams_capabilities import compute_ams_supports
from backend.app.services.bambu_mqtt import PrinterState


def _state(**overrides) -> PrinterState:
    s = PrinterState()
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def test_x1_with_ams_supports_all_four_basic_flags():
    supports = compute_ams_supports(_state(), printer_model="BL-P001")  # X1C
    assert supports["insertion_update"] is True
    assert supports["power_on_update"] is True
    assert supports["remain_capacity"] is True
    assert supports["auto_switch_filament"] is True


def test_a1_mini_lacks_rfid_so_insert_power_hidden():
    supports = compute_ams_supports(_state(), printer_model="N1")  # A1 mini, no RFID
    assert supports["insertion_update"] is False
    assert supports["power_on_update"] is False


def test_a1_mini_has_air_print_position_ams_setting():
    supports = compute_ams_supports(_state(), printer_model="N1")
    assert supports["air_print_detect"] is True


def test_x1_air_print_lives_elsewhere_so_not_in_ams_dialog():
    supports = compute_ams_supports(_state(), printer_model="BL-P001")
    # X1 has air-print in Print Options panel, NOT in AMS Settings dialog.
    assert supports["air_print_detect"] is False


def test_firmware_switch_only_for_a1_with_multifirmware():
    supports = compute_ams_supports(_state(), printer_model="N2S")  # A1 full
    assert supports["firmware_switch"] is True
    supports_x1 = compute_ams_supports(_state(), printer_model="BL-P001")
    assert supports_x1["firmware_switch"] is False


def test_reorder_for_multi_ams_h2d_only():
    supports = compute_ams_supports(_state(), printer_model="N2D")  # H2D
    assert supports["reorder"] is True
    assert compute_ams_supports(_state(), printer_model="BL-P001")["reorder"] is False


def test_all_supports_keys_always_present():
    supports = compute_ams_supports(_state(), printer_model="")
    for key in ("insertion_update", "power_on_update", "remain_capacity",
                "auto_switch_filament", "air_print_detect", "firmware_switch",
                "reorder"):
        assert key in supports
```

- [ ] **Step 2: Run tests — confirm they fail**

Run: `pytest backend/tests/unit/services/test_ams_capabilities.py -v`
Expected: ImportError or 7 FAILED.

- [ ] **Step 3: Write the helper**

```python
# backend/app/services/ams_capabilities.py
"""Derive AMS-settings row visibility from printer model + reported state.

BS computes this from a per-printer JSON in ``resources/printers/*.json``
(``support_update_remain``, ``support_filament_backup``,
``support_ams_settings_reorder``, ``air_print_detection_position``). We
inline a minimal model→capabilities table — small enough to maintain by
hand, sourced from BS:

  - X1 family (BL-P00x, C13, N2S/X2D early) — full AMS w/ RFID; air-print
    in Print Options NOT here.
  - P1S / P1P (C11, C12) — full AMS w/ RFID; air-print in Print Options.
  - A1 (N2S full firmware) — AMS w/ RFID + firmware-switch + air-print here.
  - A1 mini (N1) — AMS Lite, no RFID; air-print here.
  - H2D (N2D) — full AMS + reorder.

Extending: when BamDude meets a new printer model, append it here. Tests
in ``test_ams_capabilities.py`` lock the behaviour.
"""

from typing import TypedDict

from backend.app.services.bambu_mqtt import PrinterState


class AmsSupports(TypedDict):
    insertion_update: bool
    power_on_update: bool
    remain_capacity: bool
    auto_switch_filament: bool
    air_print_detect: bool
    firmware_switch: bool
    reorder: bool


# Family classification by model code prefix / exact match.
_X1_FAMILY = {"BL-P001", "BL-P002", "BL-P003", "BL-P004", "BL-P005",
              "C13", "X2D"}
_P1_FAMILY = {"C11", "C12"}
_A1_MINI = {"N1"}            # A1 mini — no RFID
_A1_FULL = {"N2S"}           # A1 — full AMS, firmware-switchable
_H2D = {"N2D"}               # H2D — multi-AMS + reorder


def compute_ams_supports(state: PrinterState, printer_model: str) -> AmsSupports:
    is_a1_mini = printer_model in _A1_MINI
    is_a1_full = printer_model in _A1_FULL
    is_x1 = printer_model in _X1_FAMILY
    is_p1 = printer_model in _P1_FAMILY
    is_h2d = printer_model in _H2D

    # Has RFID — anything except A1 mini's AMS Lite.
    has_rfid_ams = (is_x1 or is_p1 or is_a1_full or is_h2d)

    return AmsSupports(
        insertion_update=has_rfid_ams,
        power_on_update=has_rfid_ams,
        remain_capacity=has_rfid_ams,           # BS: support_update_remain
        auto_switch_filament=(is_x1 or is_p1 or is_a1_full or is_h2d),
        air_print_detect=(is_a1_mini or is_a1_full),  # BS: position == "ams_setting"
        firmware_switch=is_a1_full,             # BS: SupportSwitchFirmware → A1 only
        reorder=is_h2d,                         # BS: support_ams_settings_reorder
    )
```

- [ ] **Step 4: Run tests — confirm pass**

Run: `pytest backend/tests/unit/services/test_ams_capabilities.py -v`
Expected: 7 PASSED.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ams_capabilities.py \
        backend/tests/unit/services/test_ams_capabilities.py
git commit -m "feat(ams): compute_ams_supports — per-model capability gating helper"
```

---

## Task 9: Pydantic schemas `ams_settings.py`

**Files:**
- Create: `backend/app/schemas/ams_settings.py`

- [ ] **Step 1: Write schemas**

```python
# backend/app/schemas/ams_settings.py
"""Request/response schemas for /printers/{id}/ams/settings."""

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


# ---------------- Response ----------------

class AmsSystemSettingState(BaseModel):
    insertion_update: bool | None = None
    power_on_update: bool | None = None
    remain_capacity: bool | None = None
    auto_switch_filament: bool | None = None
    air_print_detect: bool | None = None
    firmware_idx_run: int | None = None
    firmware_idx_sel: int | None = None


class AmsSystemSettingSupports(BaseModel):
    insertion_update: bool = False
    power_on_update: bool = False
    remain_capacity: bool = False
    auto_switch_filament: bool = False
    air_print_detect: bool = False
    firmware_switch: bool = False
    reorder: bool = False


class AmsUnitInfo(BaseModel):
    ams_id: int
    label: str            # "AMS A", "HT-A", "Ext", per amsHelpers convention


class AmsFirmwareOption(BaseModel):
    idx: int
    label: str            # "LITE" / "FULL"


class AmsSettingsGetResponse(BaseModel):
    state: AmsSystemSettingState
    supports: AmsSystemSettingSupports
    ams_units: list[AmsUnitInfo]
    firmware_options: list[AmsFirmwareOption]


# ---------------- POST body — discriminated union ----------------

class _ActionBase(BaseModel):
    pass


class AmsUserSettingAction(_ActionBase):
    action: Literal["user_setting"]
    startup_read_option: bool
    tray_read_option: bool
    calibrate_remain_flag: bool


class AmsAutoSwitchAction(_ActionBase):
    action: Literal["auto_switch_filament"]
    enabled: bool


class AmsAirPrintAction(_ActionBase):
    action: Literal["air_print_detect"]
    enabled: bool


class AmsCalibrateAction(_ActionBase):
    action: Literal["calibrate"]
    ams_id: int = Field(ge=0, le=15)


class AmsFirmwareSwitchAction(_ActionBase):
    action: Literal["firmware_switch"]
    firmware_idx: int = Field(ge=0, le=10)


class AmsReorderAction(_ActionBase):
    action: Literal["reorder"]
    order: list[int] = Field(min_length=1, max_length=8)


AmsSettingsPostBody = Annotated[
    Union[
        AmsUserSettingAction,
        AmsAutoSwitchAction,
        AmsAirPrintAction,
        AmsCalibrateAction,
        AmsFirmwareSwitchAction,
        AmsReorderAction,
    ],
    Field(discriminator="action"),
]


class AmsSettingsPostResponse(BaseModel):
    ok: bool
    sequence_id: str | None = None
```

- [ ] **Step 2: Smoke-import**

Run: `python -c "from backend.app.schemas.ams_settings import AmsSettingsGetResponse, AmsUserSettingAction; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add backend/app/schemas/ams_settings.py
git commit -m "feat(schemas): AMS settings request/response models"
```

---

## Task 10: API router `ams_settings.py`

**Files:**
- Create: `backend/app/api/routes/ams_settings.py`
- Modify: `backend/app/main.py` (one `include_router` line)
- Create: `backend/tests/integration/api/test_ams_settings_routes.py`

- [ ] **Step 1: Write integration tests**

```python
# backend/tests/integration/api/test_ams_settings_routes.py
"""Integration tests for /printers/{id}/ams/settings GET + POST."""

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.ams_setting_audit import AmsSettingAudit
from backend.app.models.printer import Printer


@pytest.mark.asyncio
async def test_get_requires_auth(async_client):
    r = await async_client.get("/api/v1/printers/1/ams/settings")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_get_returns_state_and_supports(async_client, auth_admin, db_session):
    printer = Printer(name="t1", serial="X", model_code="BL-P001", connected=True)
    db_session.add(printer); await db_session.commit()

    with patch("backend.app.api.routes.ams_settings.printer_manager") as pm:
        client = MagicMock()
        client.state.ams_insertion_update = True
        client.state.ams_power_on_update = False
        client.state.ams_remain_capacity = None
        client.state.ams_auto_switch_filament = True
        client.state.ams_air_print_detect = None
        client.state.connected = True
        pm.get_client.return_value = client

        r = await async_client.get(
            f"/api/v1/printers/{printer.id}/ams/settings",
            headers=auth_admin,
        )
    assert r.status_code == 200
    body = r.json()
    assert body["state"]["insertion_update"] is True
    assert body["state"]["power_on_update"] is False
    assert body["state"]["remain_capacity"] is None
    assert body["supports"]["insertion_update"] is True  # X1 has RFID
    assert body["supports"]["air_print_detect"] is False  # X1 → Print Options


@pytest.mark.asyncio
async def test_post_user_setting_publishes_and_audits(async_client, auth_admin, db_session):
    printer = Printer(name="t1", serial="X", model_code="BL-P001", connected=True)
    db_session.add(printer); await db_session.commit()

    with patch("backend.app.api.routes.ams_settings.printer_manager") as pm:
        client = MagicMock()
        client.state.connected = True
        client.ams_user_setting.return_value = "SEQ-1"
        pm.get_client.return_value = client

        r = await async_client.post(
            f"/api/v1/printers/{printer.id}/ams/settings",
            headers=auth_admin,
            json={
                "action": "user_setting",
                "startup_read_option": True,
                "tray_read_option": False,
                "calibrate_remain_flag": True,
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["sequence_id"] == "SEQ-1"

    rows = (await db_session.execute(
        select(AmsSettingAudit).where(AmsSettingAudit.printer_id == printer.id)
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].action == "user_setting"
    assert rows[0].result == "sent"
    assert rows[0].sequence_id == "SEQ-1"


@pytest.mark.asyncio
async def test_post_unsupported_returns_409(async_client, auth_admin, db_session):
    """A1-mini doesn't support firmware_switch → 409 unsupported."""
    printer = Printer(name="t1", serial="X", model_code="N1", connected=True)
    db_session.add(printer); await db_session.commit()

    with patch("backend.app.api.routes.ams_settings.printer_manager") as pm:
        client = MagicMock()
        client.state.connected = True
        pm.get_client.return_value = client

        r = await async_client.post(
            f"/api/v1/printers/{printer.id}/ams/settings",
            headers=auth_admin,
            json={"action": "firmware_switch", "firmware_idx": 1},
        )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_post_offline_returns_404(async_client, auth_admin, db_session):
    printer = Printer(name="t1", serial="X", model_code="BL-P001", connected=False)
    db_session.add(printer); await db_session.commit()

    with patch("backend.app.api.routes.ams_settings.printer_manager") as pm:
        pm.get_client.return_value = None
        r = await async_client.post(
            f"/api/v1/printers/{printer.id}/ams/settings",
            headers=auth_admin,
            json={"action": "auto_switch_filament", "enabled": True},
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_post_requires_printers_update(async_client, auth_viewer, db_session):
    """Viewers have PRINTERS_READ but not PRINTERS_UPDATE → 403."""
    printer = Printer(name="t1", serial="X", model_code="BL-P001", connected=True)
    db_session.add(printer); await db_session.commit()

    r = await async_client.post(
        f"/api/v1/printers/{printer.id}/ams/settings",
        headers=auth_viewer,
        json={"action": "auto_switch_filament", "enabled": True},
    )
    assert r.status_code == 403
```

(Test fixtures `async_client`, `auth_admin`, `auth_viewer`, `db_session` are already provided by `backend/tests/conftest.py`. If a fixture name differs, adjust.)

- [ ] **Step 2: Run tests — confirm they fail (router missing)**

Run: `pytest backend/tests/integration/api/test_ams_settings_routes.py -v`
Expected: 404 or 405 on every call.

- [ ] **Step 3: Write the router**

```python
# backend/app/api/routes/ams_settings.py
"""GET / POST /printers/{printer_id}/ams/settings — AMS Settings dialog backend.

Mirrors BambuStudio's AMSSetting dialog (see spec
``docs/superpowers/specs/2026-05-12-ams-settings-dialog-design.md``). Reads
state from in-memory ``PrinterState``, writes via ``BambuMQTTClient``
publishers, and records every successful POST in ``ams_setting_audit``.
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission, get_current_user
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.ams_setting_audit import AmsSettingAudit
from backend.app.models.printer import Printer
from backend.app.models.user import User
from backend.app.schemas.ams_settings import (
    AmsAirPrintAction,
    AmsAutoSwitchAction,
    AmsCalibrateAction,
    AmsFirmwareOption,
    AmsFirmwareSwitchAction,
    AmsReorderAction,
    AmsSettingsGetResponse,
    AmsSettingsPostBody,
    AmsSettingsPostResponse,
    AmsSystemSettingState,
    AmsSystemSettingSupports,
    AmsUnitInfo,
    AmsUserSettingAction,
)
from backend.app.services import printer_manager
from backend.app.services.ams_capabilities import compute_ams_supports

router = APIRouter(prefix="/printers", tags=["ams-settings"])


@router.get("/{printer_id}/ams/settings", response_model=AmsSettingsGetResponse)
async def get_ams_settings(
    printer_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = RequirePermission(Permission.PRINTERS_READ),
):
    printer = (await db.execute(
        select(Printer).where(Printer.id == printer_id)
    )).scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    client = printer_manager.get_client(printer_id)
    if not client or not client.state.connected:
        raise HTTPException(404, "Printer not online")

    s = client.state
    state = AmsSystemSettingState(
        insertion_update=s.ams_insertion_update,
        power_on_update=s.ams_power_on_update,
        remain_capacity=s.ams_remain_capacity,
        auto_switch_filament=s.ams_auto_switch_filament,
        air_print_detect=s.ams_air_print_detect,
        firmware_idx_run=s.ams_firmware_idx_run,
        firmware_idx_sel=s.ams_firmware_idx_sel,
    )
    supports = AmsSystemSettingSupports(
        **compute_ams_supports(s, printer.model_code or "")
    )

    # ams_units derived from printer state — list each connected AMS for the
    # Calibrate dropdown. Empty for printers w/o AMS.
    ams_units = []
    for idx in (s.ams_mapping or []):
        ams_units.append(AmsUnitInfo(ams_id=idx, label=_ams_label(idx)))

    firmware_options = []
    if supports.firmware_switch:
        firmware_options = [
            AmsFirmwareOption(idx=0, label="FULL"),
            AmsFirmwareOption(idx=1, label="LITE"),
        ]

    return AmsSettingsGetResponse(
        state=state,
        supports=supports,
        ams_units=ams_units,
        firmware_options=firmware_options,
    )


@router.post("/{printer_id}/ams/settings", response_model=AmsSettingsPostResponse)
async def post_ams_settings(
    printer_id: int,
    body: AmsSettingsPostBody,
    db: AsyncSession = Depends(get_db),
    user: User = RequirePermission(Permission.PRINTERS_UPDATE),
):
    printer = (await db.execute(
        select(Printer).where(Printer.id == printer_id)
    )).scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    client = printer_manager.get_client(printer_id)
    if not client or not client.state.connected:
        raise HTTPException(404, "Printer not online")

    supports = compute_ams_supports(client.state, printer.model_code or "")

    sequence_id: str | None = None
    error: str | None = None
    try:
        if isinstance(body, AmsUserSettingAction):
            # Any of the three constituent flags supported? user_setting always
            # ships all three together but we only allow it if at least the
            # insertion/power gates pass (BS sends the same triplet regardless).
            if not (supports["insertion_update"] or supports["power_on_update"]
                    or supports["remain_capacity"]):
                raise HTTPException(409, "user_setting not supported on this printer")
            sequence_id = client.ams_user_setting(
                startup_read=body.startup_read_option,
                tray_read=body.tray_read_option,
                calibrate_remain=body.calibrate_remain_flag,
            )

        elif isinstance(body, AmsAutoSwitchAction):
            if not supports["auto_switch_filament"]:
                raise HTTPException(409, "filament backup not supported on this printer")
            sequence_id = client.print_option_auto_switch_filament(body.enabled)

        elif isinstance(body, AmsAirPrintAction):
            if not supports["air_print_detect"]:
                raise HTTPException(409, "air print detection not in AMS Settings on this printer")
            sequence_id = client.print_option_air_print_detect(body.enabled)

        elif isinstance(body, AmsCalibrateAction):
            client.ams_calibrate(body.ams_id)

        elif isinstance(body, AmsFirmwareSwitchAction):
            if not supports["firmware_switch"]:
                raise HTTPException(409, "firmware switch not supported on this printer")
            sequence_id = client.ams_firmware_switch(body.firmware_idx)

        elif isinstance(body, AmsReorderAction):
            if not supports["reorder"]:
                raise HTTPException(409, "AMS reorder not supported on this printer")
            sequence_id = client.ams_reorder(body.order)

        result = "sent"
    except HTTPException:
        raise
    except Exception as exc:  # MQTT publish failure
        error = str(exc)
        result = "error"

    db.add(AmsSettingAudit(
        printer_id=printer_id,
        user_id=user.id if user else None,
        action=body.action,
        payload_json=json.dumps(body.model_dump(mode="json")),
        sequence_id=sequence_id,
        result=result,
        error_message=error,
    ))
    await db.commit()

    if result == "error":
        raise HTTPException(504, error or "MQTT publish failed")

    return AmsSettingsPostResponse(ok=True, sequence_id=sequence_id)


def _ams_label(ams_id: int) -> str:
    """Mirror frontend's amsHelpers.formatSlotLabel for the unit selector."""
    if ams_id == 255:
        return "External"
    if ams_id >= 128:
        return f"HT-{chr(ord('A') + (ams_id - 128))}"
    return f"AMS {chr(ord('A') + ams_id)}"
```

If `client.ams_user_setting` etc. are `async def` (per Task 4 step 3), `await` them.

- [ ] **Step 4: Register the router**

Open `backend/app/main.py`. Find the block where `app.include_router(...)` calls live for printer-related routers. Add:

```python
from backend.app.api.routes import ams_settings as ams_settings_router  # near other route imports

# ... later, near the other ams-prefixed include lines:
app.include_router(ams_settings_router.router, prefix="/api/v1")
```

- [ ] **Step 5: Run tests — confirm pass**

Run: `pytest backend/tests/integration/api/test_ams_settings_routes.py -v`
Expected: 5 PASSED.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/routes/ams_settings.py backend/app/main.py \
        backend/tests/integration/api/test_ams_settings_routes.py
git commit -m "feat(api): /printers/{id}/ams/settings GET + POST with audit"
```

---

## Task 11: Frontend API client + hook

**Files:**
- Modify: `frontend/src/api/client.ts` — two methods.
- Create: `frontend/src/hooks/useAmsSettings.ts`

- [ ] **Step 1: Add the two API methods to client.ts**

Find the existing AMS-related methods (`grep -n "ams" frontend/src/api/client.ts`). Add nearby:

```typescript
// Mirrors backend/app/schemas/ams_settings.py — keep in sync.
export interface AmsSystemSettingState {
  insertion_update: boolean | null;
  power_on_update: boolean | null;
  remain_capacity: boolean | null;
  auto_switch_filament: boolean | null;
  air_print_detect: boolean | null;
  firmware_idx_run: number | null;
  firmware_idx_sel: number | null;
}

export interface AmsSystemSettingSupports {
  insertion_update: boolean;
  power_on_update: boolean;
  remain_capacity: boolean;
  auto_switch_filament: boolean;
  air_print_detect: boolean;
  firmware_switch: boolean;
  reorder: boolean;
}

export interface AmsUnitInfo { ams_id: number; label: string }
export interface AmsFirmwareOption { idx: number; label: string }

export interface AmsSettingsGetResponse {
  state: AmsSystemSettingState;
  supports: AmsSystemSettingSupports;
  ams_units: AmsUnitInfo[];
  firmware_options: AmsFirmwareOption[];
}

export type AmsSettingsPostBody =
  | { action: 'user_setting'; startup_read_option: boolean; tray_read_option: boolean; calibrate_remain_flag: boolean }
  | { action: 'auto_switch_filament'; enabled: boolean }
  | { action: 'air_print_detect'; enabled: boolean }
  | { action: 'calibrate'; ams_id: number }
  | { action: 'firmware_switch'; firmware_idx: number }
  | { action: 'reorder'; order: number[] };

export const getAmsSettings = (printerId: number) =>
  request<AmsSettingsGetResponse>(`/printers/${printerId}/ams/settings`);

export const postAmsSettings = (printerId: number, body: AmsSettingsPostBody) =>
  request<{ ok: boolean; sequence_id: string | null }>(
    `/printers/${printerId}/ams/settings`,
    { method: 'POST', body: JSON.stringify(body) },
  );
```

- [ ] **Step 2: Write the hook**

```typescript
// frontend/src/hooks/useAmsSettings.ts
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useRef } from 'react';

import {
  AmsSettingsGetResponse, AmsSettingsPostBody,
  getAmsSettings, postAmsSettings,
} from '../api/client';

const QK = (printerId: number) => ['ams-settings', printerId] as const;

/**
 * Client-side hold-timer: when we POST a change, we record which flag was
 * touched and for how long (3s). The modal reads this map and prefers its
 * own optimistic value over WS-driven refetches during the hold window —
 * mirrors the backend hold so the UI doesn't blink.
 */
export function useAmsSettings(printerId: number) {
  const qc = useQueryClient();
  const holdsRef = useRef<Map<string, number>>(new Map());

  const query = useQuery({
    queryKey: QK(printerId),
    queryFn: () => getAmsSettings(printerId),
    staleTime: 5_000,
  });

  const mutation = useMutation({
    mutationFn: (body: AmsSettingsPostBody) => postAmsSettings(printerId, body),
    onSuccess: (_data, body) => {
      const now = Date.now();
      for (const flag of flagsForAction(body)) {
        holdsRef.current.set(flag, now + 3_000);
      }
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

function flagsForAction(body: AmsSettingsPostBody): string[] {
  switch (body.action) {
    case 'user_setting':
      return ['insertion_update', 'power_on_update', 'remain_capacity'];
    case 'auto_switch_filament':
      return ['auto_switch_filament'];
    case 'air_print_detect':
      return ['air_print_detect'];
    default:
      return [];
  }
}
```

- [ ] **Step 3: tsc smoke**

Run: `cd frontend && npx tsc --noEmit`
Expected: no new errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/client.ts frontend/src/hooks/useAmsSettings.ts
git commit -m "feat(frontend): AMS settings API client + useAmsSettings hook with hold-timer"
```

---

## Task 12: i18n keys (en + uk from BS)

**Files:**
- Modify: `frontend/src/i18n/locales/en.ts`
- Modify: `frontend/src/i18n/locales/uk.ts`

- [ ] **Step 1: Find the right place to insert the new block**

Run: `grep -n "ams:" frontend/src/i18n/locales/en.ts | head -5` — locate an existing `ams` namespace (if any) or pick a sibling block to anchor before.

- [ ] **Step 2: Extract UK strings from `BambuStudio_uk.po`**

```bash
grep -A1 "msgid \"AMS Settings\"" temp/references/BambuStudio/bbl/i18n/uk/BambuStudio_uk.po
grep -A1 "msgid \"Insertion update\"" temp/references/BambuStudio/bbl/i18n/uk/BambuStudio_uk.po
# ... repeat for each msgid below
```

Build a list of `(msgid_en, msgstr_uk)` pairs for these BS strings:
- `AMS Settings`
- `Insertion update`
- `The AMS will automatically read the filament information when inserting a new Bambu Lab filament. This takes about 20 seconds.`
- `Note: if a new filament is inserted during  printing, the AMS will not automatically read any information until printing is completed.` (note: double space)
- `When inserting a new filament, the AMS will not automatically read its information, leaving it blank for you to enter manually.`
- `Power on update`
- `The AMS will automatically read the information of inserted filament on start-up. It will take about 1 minute.The reading process will roll filament spools.`
- `The AMS will not automatically read information from inserted filament during startup and will continue to use the information recorded before the last shutdown.`
- `Update remaining capacity`
- `AMS will attempt to estimate the remaining capacity of the Bambu Lab filaments.`
- `AMS filament backup`
- `AMS will continue to another spool with the same properties of filament automatically when current filament runs out`
- `Air Printing Detection`
- `Detects clogging and filament grinding, halting printing immediately to conserve time and filament.`
- `AMS Type`
- `Arrange AMS Order`
- `If you want a specific AMS ID sequence, please disconnect all AMS after clicking 'Reset', and then reconnect them in the desired order.`
- `Reset`
- `Are you sure to reset the ID sequence of the connected AMS?`
- `Reset AMS Sequence`
- `The printer is busy and cannot switch AMS type.`
- `Please unload all filament before switching.`
- `AMS type switching needs firmware update, taking about 30s. Switch now ?`
- `Confirm`

For any `msgstr ""` (empty translation) — leave the value as the English string + `// TBD-uk` comment so it's visible.

- [ ] **Step 3: Add the block to both locale files**

In `en.ts`, in the appropriate location:

```typescript
amsSettings: {
  title: "AMS Settings",
  insertionUpdate: "Insertion update",
  insertionUpdateTipOn:
    "The AMS will automatically read the filament information when inserting a new Bambu Lab filament. This takes about 20 seconds.",
  insertionUpdateTipNote:
    "Note: if a new filament is inserted during printing, the AMS will not automatically read any information until printing is completed.",
  insertionUpdateTipOff:
    "When inserting a new filament, the AMS will not automatically read its information, leaving it blank for you to enter manually.",
  powerOnUpdate: "Power on update",
  powerOnTipOn:
    "The AMS will automatically read the information of inserted filament on start-up. It will take about 1 minute. The reading process will roll filament spools.",
  powerOnTipOff:
    "The AMS will not automatically read information from inserted filament during startup and will continue to use the information recorded before the last shutdown.",
  updateRemain: "Update remaining capacity",
  updateRemainTip: "AMS will attempt to estimate the remaining capacity of the Bambu Lab filaments.",
  filamentBackup: "AMS filament backup",
  filamentBackupTip:
    "AMS will continue to another spool with the same properties of filament automatically when current filament runs out",
  airPrintDetection: "Air Printing Detection",
  airPrintTip:
    "Detects clogging and filament grinding, halting printing immediately to conserve time and filament.",
  amsType: "AMS Type",
  arrangeOrder: "Arrange AMS Order",
  arrangeNote:
    "If you want a specific AMS ID sequence, please disconnect all AMS after clicking 'Reset', and then reconnect them in the desired order.",
  reset: "Reset",
  confirmReorder: "Are you sure to reset the ID sequence of the connected AMS?",
  reorderTitle: "Reset AMS Sequence",
  busyCantSwitch: "The printer is busy and cannot switch AMS type.",
  unloadBeforeSwitch: "Please unload all filament before switching.",
  switchFirmwareConfirm: "AMS type switching needs firmware update, taking about 30s. Switch now?",
  switchProgress: "Switching {{percent}}%",
  confirm: "Confirm",
  calibrate: "Calibrate AMS",
  selectAmsForCalibrate: "AMS unit",
  readOnlyBanner: "Read-only — requires Manage permission",
  waitingForPrinter: "Waiting for printer status",
},
```

In `uk.ts`, paste the same key list with values from BS `BambuStudio_uk.po`. For any empty `msgstr`, keep the English value and add `// TBD-uk` comment — the implementer will surface those in their PR description so the user can decide.

- [ ] **Step 4: tsc smoke**

Run: `cd frontend && npx tsc --noEmit && npm run lint`
Expected: no new errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/i18n/locales/en.ts frontend/src/i18n/locales/uk.ts
git commit -m "feat(i18n): amsSettings block en+uk from BambuStudio _L() strings"
```

---

## Task 13: `AMSSettingsModal.tsx` component

**Files:**
- Create: `frontend/src/components/AMSSettingsModal.tsx`
- Create: `frontend/src/__tests__/components/AMSSettingsModal.test.tsx`

- [ ] **Step 1: Write component tests**

```tsx
// frontend/src/__tests__/components/AMSSettingsModal.test.tsx
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, it, expect, vi, beforeEach } from 'vitest';

import { AMSSettingsModal } from '../../components/AMSSettingsModal';
import * as api from '../../api/client';

vi.mock('../../api/client');

function renderWith(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const fullSupports = {
  insertion_update: true, power_on_update: true, remain_capacity: true,
  auto_switch_filament: true, air_print_detect: false,
  firmware_switch: false, reorder: false,
};

beforeEach(() => {
  vi.mocked(api.getAmsSettings).mockResolvedValue({
    state: {
      insertion_update: true, power_on_update: false, remain_capacity: true,
      auto_switch_filament: true, air_print_detect: null,
      firmware_idx_run: null, firmware_idx_sel: null,
    },
    supports: fullSupports,
    ams_units: [{ ams_id: 0, label: 'AMS A' }],
    firmware_options: [],
  });
  vi.mocked(api.postAmsSettings).mockResolvedValue({ ok: true, sequence_id: 'S1' });
});

describe('AMSSettingsModal', () => {
  it('renders nothing when closed', () => {
    renderWith(<AMSSettingsModal isOpen={false} onClose={() => {}} printerId={1} />);
    expect(screen.queryByText(/AMS Settings/i)).toBeNull();
  });

  it('shows skeleton while loading then renders supported rows', async () => {
    renderWith(<AMSSettingsModal isOpen={true} onClose={() => {}} printerId={1} />);
    await waitFor(() => expect(screen.getByText('Insertion update')).toBeInTheDocument());
    expect(screen.getByText('Power on update')).toBeInTheDocument();
    expect(screen.getByText('AMS filament backup')).toBeInTheDocument();
    // air_print_detect=false → row hidden
    expect(screen.queryByText('Air Printing Detection')).toBeNull();
  });

  it('toggling Insertion update calls postAmsSettings with all three flags', async () => {
    renderWith(<AMSSettingsModal isOpen={true} onClose={() => {}} printerId={1} />);
    const cb = await screen.findByLabelText('Insertion update');
    fireEvent.click(cb);
    await waitFor(() => {
      expect(api.postAmsSettings).toHaveBeenCalledWith(1, expect.objectContaining({
        action: 'user_setting',
        tray_read_option: false,             // was true, now toggled off
        startup_read_option: false,          // unchanged
        calibrate_remain_flag: true,         // unchanged
      }));
    });
  });

  it('reverts on API error and shows a toast', async () => {
    vi.mocked(api.postAmsSettings).mockRejectedValueOnce(new Error('boom'));
    renderWith(<AMSSettingsModal isOpen={true} onClose={() => {}} printerId={1} />);
    const cb = await screen.findByLabelText('Insertion update');
    fireEvent.click(cb);
    await waitFor(() => expect(cb).toBeChecked()); // reverted to original
  });
});
```

- [ ] **Step 2: Run tests — confirm they fail (component missing)**

Run: `cd frontend && npm run test:run -- AMSSettingsModal`
Expected: ENOENT / import error.

- [ ] **Step 3: Write the component**

```tsx
// frontend/src/components/AMSSettingsModal.tsx
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { XMarkIcon } from '@heroicons/react/24/outline';
import { toast } from 'react-hot-toast';

import { useAmsSettings } from '../hooks/useAmsSettings';
import type { AmsSystemSettingState, AmsSettingsPostBody } from '../api/client';

interface Props {
  isOpen: boolean;
  onClose: () => void;
  printerId: number;
}

type LocalState = AmsSystemSettingState;

export function AMSSettingsModal({ isOpen, onClose, printerId }: Props) {
  const { t } = useTranslation();
  const { data, isLoading, mutate } = useAmsSettings(printerId);
  const [localState, setLocalState] = useState<LocalState | null>(null);
  const [selectedAmsId, setSelectedAmsId] = useState<number>(0);
  const [reorderConfirm, setReorderConfirm] = useState(false);
  const [fwSwitchConfirm, setFwSwitchConfirm] = useState<number | null>(null);

  // Sync server state into local copy once it lands.
  useEffect(() => {
    if (data?.state) setLocalState(data.state);
  }, [data?.state]);

  if (!isOpen) return null;

  const supports = data?.supports;
  const s = localState ?? data?.state;

  const submit = async (body: AmsSettingsPostBody, optimistic: Partial<LocalState>) => {
    if (!localState) return;
    const prev = localState;
    setLocalState({ ...localState, ...optimistic });
    try {
      await mutate(body);
    } catch (e) {
      setLocalState(prev);
      toast.error((e as Error).message ?? 'Request failed');
    }
  };

  const onToggleInsertion = (next: boolean) => {
    if (!s) return;
    submit(
      {
        action: 'user_setting',
        tray_read_option: next,
        startup_read_option: !!s.power_on_update,
        calibrate_remain_flag: !!s.remain_capacity,
      },
      { insertion_update: next },
    );
  };
  const onTogglePowerOn = (next: boolean) => {
    if (!s) return;
    submit(
      {
        action: 'user_setting',
        tray_read_option: !!s.insertion_update,
        startup_read_option: next,
        calibrate_remain_flag: !!s.remain_capacity,
      },
      { power_on_update: next },
    );
  };
  const onToggleRemain = (next: boolean) => {
    if (!s) return;
    submit(
      {
        action: 'user_setting',
        tray_read_option: !!s.insertion_update,
        startup_read_option: !!s.power_on_update,
        calibrate_remain_flag: next,
      },
      { remain_capacity: next },
    );
  };
  const onToggleBackup = (next: boolean) =>
    submit({ action: 'auto_switch_filament', enabled: next }, { auto_switch_filament: next });
  const onToggleAirPrint = (next: boolean) =>
    submit({ action: 'air_print_detect', enabled: next }, { air_print_detect: next });

  const onCalibrate = () => mutate({ action: 'calibrate', ams_id: selectedAmsId });

  const onConfirmFwSwitch = () => {
    if (fwSwitchConfirm == null) return;
    mutate({ action: 'firmware_switch', firmware_idx: fwSwitchConfirm });
    setFwSwitchConfirm(null);
  };

  const onConfirmReorder = () => {
    mutate({ action: 'reorder', order: data?.ams_units.map(u => u.ams_id) ?? [] });
    setReorderConfirm(false);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl w-full max-w-md mx-4 max-h-[90vh] overflow-y-auto">
        <div className="flex justify-between items-center p-4 border-b dark:border-gray-700">
          <h2 className="text-lg font-semibold">{t('amsSettings.title')}</h2>
          <button onClick={onClose} aria-label="Close">
            <XMarkIcon className="h-5 w-5" />
          </button>
        </div>

        {isLoading || !s || !supports ? (
          <div className="p-6 space-y-3">
            {[...Array(4)].map((_, i) => (
              <div key={i} className="animate-pulse h-12 bg-gray-200 dark:bg-gray-700 rounded" />
            ))}
          </div>
        ) : (
          <div className="p-4 space-y-4">
            {supports.insertion_update && (
              <Row
                title={t('amsSettings.insertionUpdate')}
                tip={
                  s.insertion_update
                    ? `${t('amsSettings.insertionUpdateTipOn')} ${t('amsSettings.insertionUpdateTipNote')}`
                    : t('amsSettings.insertionUpdateTipOff')
                }
                checked={!!s.insertion_update}
                onChange={onToggleInsertion}
              />
            )}
            {supports.power_on_update && (
              <Row
                title={t('amsSettings.powerOnUpdate')}
                tip={s.power_on_update ? t('amsSettings.powerOnTipOn') : t('amsSettings.powerOnTipOff')}
                checked={!!s.power_on_update}
                onChange={onTogglePowerOn}
              />
            )}
            {supports.remain_capacity && (
              <Row
                title={t('amsSettings.updateRemain')}
                tip={t('amsSettings.updateRemainTip')}
                checked={!!s.remain_capacity}
                onChange={onToggleRemain}
              />
            )}
            {supports.auto_switch_filament && (
              <Row
                title={t('amsSettings.filamentBackup')}
                tip={t('amsSettings.filamentBackupTip')}
                checked={!!s.auto_switch_filament}
                onChange={onToggleBackup}
              />
            )}
            {supports.air_print_detect && (
              <Row
                title={t('amsSettings.airPrintDetection')}
                tip={t('amsSettings.airPrintTip')}
                checked={!!s.air_print_detect}
                onChange={onToggleAirPrint}
              />
            )}

            {supports.firmware_switch && data && (
              <div className="border-t dark:border-gray-700 pt-3">
                <div className="font-medium">{t('amsSettings.amsType')}</div>
                <div className="mt-2 flex gap-2 items-center">
                  <select
                    className="border rounded px-2 py-1 dark:bg-gray-700"
                    value={s.firmware_idx_sel ?? data.firmware_options[0]?.idx ?? 0}
                    onChange={(e) => setFwSwitchConfirm(Number(e.target.value))}
                  >
                    {data.firmware_options.map(o => (
                      <option key={o.idx} value={o.idx}>{o.label}</option>
                    ))}
                  </select>
                </div>
              </div>
            )}

            {supports.reorder && (
              <div className="border-t dark:border-gray-700 pt-3">
                <div className="font-medium">{t('amsSettings.arrangeOrder')}</div>
                <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
                  {t('amsSettings.arrangeNote')}
                </p>
                <button
                  className="mt-2 px-3 py-1 border rounded hover:bg-gray-100 dark:hover:bg-gray-700"
                  onClick={() => setReorderConfirm(true)}
                >
                  {t('amsSettings.reset')}
                </button>
              </div>
            )}

            {(data?.ams_units?.length ?? 0) > 0 && (
              <div className="border-t dark:border-gray-700 pt-3">
                <div className="font-medium">{t('amsSettings.calibrate')}</div>
                <div className="mt-2 flex gap-2 items-center">
                  <label className="text-sm">{t('amsSettings.selectAmsForCalibrate')}:</label>
                  <select
                    className="border rounded px-2 py-1 dark:bg-gray-700"
                    value={selectedAmsId}
                    onChange={(e) => setSelectedAmsId(Number(e.target.value))}
                  >
                    {data!.ams_units.map(u => (
                      <option key={u.ams_id} value={u.ams_id}>{u.label}</option>
                    ))}
                  </select>
                  <button
                    className="px-3 py-1 border rounded hover:bg-gray-100 dark:hover:bg-gray-700"
                    onClick={onCalibrate}
                  >
                    {t('amsSettings.calibrate')}
                  </button>
                </div>
              </div>
            )}
          </div>
        )}

        {reorderConfirm && (
          <ConfirmDialog
            title={t('amsSettings.reorderTitle')}
            body={t('amsSettings.confirmReorder')}
            confirmLabel={t('amsSettings.confirm')}
            onConfirm={onConfirmReorder}
            onCancel={() => setReorderConfirm(false)}
          />
        )}

        {fwSwitchConfirm != null && (
          <ConfirmDialog
            title={t('amsSettings.amsType')}
            body={t('amsSettings.switchFirmwareConfirm')}
            confirmLabel={t('amsSettings.confirm')}
            onConfirm={onConfirmFwSwitch}
            onCancel={() => setFwSwitchConfirm(null)}
          />
        )}
      </div>
    </div>
  );
}

function Row({ title, tip, checked, onChange }: {
  title: string; tip: string; checked: boolean; onChange: (v: boolean) => void;
}) {
  return (
    <label className="flex items-start gap-3 cursor-pointer">
      <input
        type="checkbox"
        className="mt-1 h-4 w-4"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        aria-label={title}
      />
      <div className="flex-1">
        <div className="font-medium">{title}</div>
        <p className="text-sm text-gray-500 dark:text-gray-400">{tip}</p>
      </div>
    </label>
  );
}

function ConfirmDialog({ title, body, confirmLabel, onConfirm, onCancel }: {
  title: string; body: string; confirmLabel: string;
  onConfirm: () => void; onCancel: () => void;
}) {
  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60">
      <div className="bg-white dark:bg-gray-800 rounded shadow-xl p-4 max-w-sm">
        <h3 className="font-semibold">{title}</h3>
        <p className="mt-2 text-sm">{body}</p>
        <div className="mt-4 flex justify-end gap-2">
          <button className="px-3 py-1 border rounded" onClick={onCancel}>Cancel</button>
          <button className="px-3 py-1 bg-blue-600 text-white rounded" onClick={onConfirm}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Run tests — confirm pass**

Run: `cd frontend && npm run test:run -- AMSSettingsModal`
Expected: 4 PASSED.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/AMSSettingsModal.tsx \
        frontend/src/__tests__/components/AMSSettingsModal.test.tsx
git commit -m "feat(frontend): AMSSettingsModal component with supports-driven row visibility"
```

---

## Task 14: Wire gear icon into PrintersPage

**Files:**
- Modify: `frontend/src/pages/PrintersPage.tsx`

- [ ] **Step 1: Locate the AMS panel header**

Run: `grep -n "ams\|AMS" frontend/src/pages/PrintersPage.tsx | head -20`

Find the JSX block that renders the AMS card/section for a printer. Note the variable holding the current printer (`p`, `printer`, `currentPrinter` — depends on local code).

- [ ] **Step 2: Add a gear button next to the AMS header**

```tsx
import { Cog6ToothIcon } from '@heroicons/react/24/outline';
import { AMSSettingsModal } from '../components/AMSSettingsModal';
import { useAuth } from '../contexts/AuthContext';   // already imported? reuse existing
// ...

// Inside the component body:
const [amsSettingsOpenFor, setAmsSettingsOpenFor] = useState<number | null>(null);
const auth = useAuth();
const canManage = auth.user?.permissions?.includes('printers:update');

// Inside the AMS section JSX header:
{canManage && printer.connected && printer.ams_exist_bits !== 0 && (
  <button
    aria-label="AMS Settings"
    onClick={() => setAmsSettingsOpenFor(printer.id)}
    className="ml-2 p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700"
  >
    <Cog6ToothIcon className="h-4 w-4" />
  </button>
)}

// At the end of the component, near other modal mounts:
<AMSSettingsModal
  isOpen={amsSettingsOpenFor != null}
  onClose={() => setAmsSettingsOpenFor(null)}
  printerId={amsSettingsOpenFor ?? 0}
/>
```

- [ ] **Step 3: tsc + lint + dev sanity**

```bash
cd frontend && npx tsc --noEmit && npm run lint
```

Then open a dev server (`npm run dev`) and visually confirm: a printer with AMS shows a gear icon; clicking opens the modal; toggling a checkbox doesn't throw a console error (real MQTT activity requires a connected printer — without one, the POST will 404 with "Printer not online", which is fine for smoke).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/PrintersPage.tsx
git commit -m "feat(frontend): gear icon + AMSSettingsModal mount on AMS panel"
```

---

## Task 15: Build + CHANGELOG

**Files:**
- Modify: `frontend/static/*` (generated)
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Frontend build**

Run: `cd frontend && npm run build`
Expected: clean build, new bundle in `static/`.

- [ ] **Step 2: CHANGELOG entry**

Open `CHANGELOG.md`. Find the existing `## [0.4.4] - <date>` section (per the user's standing rule: this cycle lands under 0.4.4). Add under the `### Added` or `### Features` sub-heading:

```markdown
- feat(ams): AMS Settings dialog — gear icon on each AMS panel opens a Bambu Studio-parity modal. Toggle insertion / power-on RFID auto-read, remaining-capacity estimation, AMS filament backup, air-print detection (A1 only). Calibrate an AMS unit. Switch A1 firmware between LITE and FULL. Rearrange AMS unit IDs (H2D). All gated by `printers:update`; every change recorded in `ams_setting_audit`. (#NNN)
```

(Replace `(#NNN)` with the actual PR number when it's opened, or remove if not tracked.)

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md frontend/static
git commit -m "chore(release): AMS Settings dialog bundle + CHANGELOG"
```

---

## Task 16: Full verify pass + manual hardware check

- [ ] **Step 1: Backend**

```bash
cd D:/Development/bamdude
ruff check backend/
pytest backend/tests/unit/services/test_bambu_mqtt_ams_settings.py \
       backend/tests/unit/services/test_ams_capabilities.py \
       backend/tests/unit/migrations/test_m060_ams_setting_audit.py \
       backend/tests/integration/api/test_ams_settings_routes.py -v
```

Expected: ruff clean, all 4 test files pass.

- [ ] **Step 2: Frontend**

```bash
cd frontend && npm run lint && npx tsc --noEmit && npm run test:run
```

Expected: clean.

- [ ] **Step 3: Manual hardware check (one printer minimum)**

Spin up the dev backend (`DEBUG=true uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000`) against a real Bambu printer in the lab. With the frontend dev server pointing to it:

1. Open the printer page → gear icon visible on AMS panel.
2. Click gear → modal opens, all `supports` rows match printer model.
3. Toggle Insertion update OFF → MQTT log shows `ams_user_setting` with `tray_read_option: false`. Bambu Studio (if also connected) sees the same toggle update within 5s.
4. Toggle Backup → log shows `print_option` with `auto_switch_filament`.
5. Click Calibrate (with at least one AMS connected) → printer beeps and starts the standard AMS calibration sweep.
6. (If model supports) firmware_switch and reorder — pre-verify with `unload` first.
7. Check `SELECT * FROM ams_setting_audit ORDER BY id DESC LIMIT 10;` — one row per click, `result='sent'`.

Document any unexpected behavior as a follow-up issue. Do NOT mark this step complete until at least the four basic toggles confirm round-trip through a real printer.

- [ ] **Step 4: Final commit (if any cleanup landed)**

```bash
git status
# only commit if anything new staged
```

---

## Self-Review

**Spec coverage:**
- §4 Architecture → Tasks 3-5 (state), 4+7 (publishers), 10 (router), 13-14 (UI), 1-2 (migration+model). ✓
- §5 Actions → Tasks 4 (4 simple) + 7 (2 research-gated). ✓
- §6 State reads → Tasks 3+5. ✓
- §7 Backend → Tasks 1-2 (migration+model), 8 (capabilities), 9 (schemas), 10 (router). ✓
- §8 Frontend → Tasks 11-14. ✓
- §9 i18n → Task 12. ✓
- §10 Errors/edge cases → covered in Task 10 tests + Task 13 component. ✓
- §11 Open questions → Task 6 (research) before Task 7 (implementation). ✓
- §12 Tests → Tasks 1, 4, 5, 8, 10, 13 + Task 16 (manual). ✓
- §13 Rollout → Task 15 (CHANGELOG + build). ✓

**Placeholder scan:**
- `(#NNN)` in CHANGELOG step 2 — explicitly flagged as "replace when PR opens", not a plan-time TODO. Acceptable.
- `<command_from_task6>` placeholder in Task 7 step 1 — intentional, depends on Task 6 research output. Acceptable.
- Other "TBD-uk" markers in i18n step 3 — these are inline TODOs in the *artifact* (locale file) so the user reviews them in the PR. Acceptable.
- No "TODO", "implement later", "similar to" found.

**Type consistency:**
- `AmsSystemSettingState` field names — `insertion_update / power_on_update / remain_capacity / auto_switch_filament / air_print_detect` — match between schemas, hook, modal, and tests. ✓
- `AmsSettingsPostBody` action discriminators — `user_setting / auto_switch_filament / air_print_detect / calibrate / firmware_switch / reorder` — match across schemas, client, hook, router, tests. ✓
- MQTT publisher names — `ams_user_setting / print_option_auto_switch_filament / print_option_air_print_detect / ams_calibrate / ams_firmware_switch / ams_reorder` — match in Tasks 4, 7, 10. ✓
- Permission — `PRINTERS_UPDATE` (resolved from spec's `PRINTERS_MANAGE`) used consistently in Task 10 + Task 14 client gate (`printers:update`). ✓

No gaps found.
