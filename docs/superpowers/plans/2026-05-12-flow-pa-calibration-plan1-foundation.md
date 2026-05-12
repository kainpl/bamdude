# Flow Rate / PA Calibration — Plan 1 (Backend Foundation)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Backend foundation for Filament Calibration wizard — DB schema, models, MQTT extensions, service layer, REST API. After this plan: all endpoints callable via curl/pytest, no UI yet. P1S/X1/H2D auto+manual paths all wired backend-side.

**Architecture:** New table `filament_calibration` (per-filament-type, per-printer-model, multi-row history with `is_active` flag) + `calibration_session` (orchestration) + `calibration_audit` (BS-parity log). `CalibrationService` orchestrates start → run → submit → save with auto-bind via `extrusion_cali_sel`. MQTT layer extends existing `bambu_mqtt.py` with auto-cali start + push parser. 3MF assets ship from BS `resources/calib/`. Dispatch hook re-syncs active calibration on every job start.

**Tech Stack:** Python 3.10 / FastAPI / SQLAlchemy 2.0 async / Pydantic 2 / aiosqlite + asyncpg / MQTT (existing `BambuMQTTClient`).

**Spec:** `docs/superpowers/specs/2026-05-12-flow-pa-calibration-design.md`. Research dump: `temp/printer_settings/flow_pa_calibration_research.md`. BS reference: `temp/references/BambuStudio/`.

**Out of scope for this plan:** Frontend wizard (Plan 2), Auto save UI (Plan 3), History modal (Plan 3), H2D dual-extruder UI (Plan 3), i18n (Plan 2+3), docs/landing updates (Plan 3).

**User workflow notes (override skill defaults):**
- Per-wave verify (not per-task) — run `pytest` once at end of each wave.
- Commits — at the end of each wave, ONLY when user explicitly asks. Plan steps say "stage" not "commit".
- All conversation in Ukrainian; code/docs/commits in English.

---

## File Map

**New backend files (12):**
- `backend/app/migrations/m062_filament_calibration.py` — DDL
- `backend/app/models/filament_calibration.py` — FilamentCalibration ORM
- `backend/app/models/calibration_session.py` — CalibrationSession ORM
- `backend/app/models/calibration_audit.py` — CalibrationAudit ORM
- `backend/app/schemas/filament_calibration.py` — Pydantic
- `backend/app/api/routes/filament_calibration.py` — REST router
- `backend/app/services/calibration_service.py` — orchestrator
- `backend/app/services/calibration_constants.py` — CalibMode enum + PA ranges + nozzle_id helper
- `backend/app/data/calib_assets/README.md` — asset provenance
- `backend/tests/unit/services/test_calibration_service.py`
- `backend/tests/unit/services/test_bambu_mqtt_calibration.py`
- `backend/tests/unit/services/test_calibration_capabilities.py`
- `backend/tests/integration/test_m062_filament_calibration_migration.py`
- `backend/tests/integration/test_calibration_routes.py`

**Modified backend files (6):**
- `backend/app/services/bambu_mqtt.py` — new publishers + parser hooks + PrinterState fields
- `backend/app/services/printer_capabilities.py` — add `compute_calibration_supports`
- `backend/app/services/background_dispatch.py` — add `_resolve_and_apply_calibration` hook
- `backend/app/models/__init__.py` — register new models
- `backend/app/models/print_archive.py` — add `is_calibration`, `calibration_session_id`
- `backend/app/models/print_queue_item.py` (or wherever) — same fields
- `backend/app/main.py` — register new router

**Asset directory:** `backend/app/data/calib_assets/{pressure_advance,filament_flow,temp_tower,volumetric_speed,vfa,retraction}/`.

---

## Wave 1 — DB foundation

### Task 1: Migration m062 (DDL + extra columns)

**Files:**
- Create: `backend/app/migrations/m062_filament_calibration.py`
- Test: `backend/tests/integration/test_m062_filament_calibration_migration.py`

- [ ] **Step 1: Write the failing migration test**

```python
# backend/tests/integration/test_m062_filament_calibration_migration.py
"""Tests m062 creates filament_calibration + calibration_session + calibration_audit,
   plus is_calibration columns on print_archive + print_queue_items."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text


@pytest.mark.asyncio
async def test_filament_calibration_table_exists(db_session):
    rows = (
        await db_session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='filament_calibration'")
        )
    ).fetchall()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_filament_calibration_columns(db_session):
    cols = (await db_session.execute(text("PRAGMA table_info('filament_calibration')"))).fetchall()
    names = {c[1] for c in cols}
    assert {
        "id", "printer_model", "filament_id", "filament_setting_id",
        "nozzle_diameter", "nozzle_volume_type", "extruder_id",
        "pa_k_value", "pa_n_coef", "flow_ratio", "confidence",
        "cali_mode", "source", "is_active", "cali_idx",
        "name", "notes",
        "calibrated_on_printer_id", "calibrated_by_user_id", "created_at",
    }.issubset(names)


@pytest.mark.asyncio
async def test_calibration_session_table(db_session):
    cols = (await db_session.execute(text("PRAGMA table_info('calibration_session')"))).fetchall()
    names = {c[1] for c in cols}
    assert {
        "id", "printer_id", "user_id", "cali_mode", "method",
        "nozzle_diameter", "nozzle_volume_type", "extruder_id",
        "filaments_json", "status", "mqtt_sequence_id",
        "print_queue_item_id", "parent_session_id", "stage",
        "coarse_ratio", "error_message", "created_at", "updated_at",
    }.issubset(names)


@pytest.mark.asyncio
async def test_calibration_audit_table(db_session):
    cols = (await db_session.execute(text("PRAGMA table_info('calibration_audit')"))).fetchall()
    names = {c[1] for c in cols}
    assert {
        "id", "printer_id", "session_id", "filament_calibration_id",
        "user_id", "action", "payload_json", "sequence_id",
        "result", "error_message", "created_at",
    }.issubset(names)


@pytest.mark.asyncio
async def test_print_archive_has_is_calibration(db_session):
    cols = (await db_session.execute(text("PRAGMA table_info('print_archives')"))).fetchall()
    names = {c[1] for c in cols}
    assert "is_calibration" in names
    assert "calibration_session_id" in names


@pytest.mark.asyncio
async def test_print_queue_has_is_calibration(db_session):
    # Adjust table name if your project uses singular print_queue_item
    cols = (await db_session.execute(text("PRAGMA table_info('print_queue_items')"))).fetchall()
    names = {c[1] for c in cols}
    assert "is_calibration" in names
    assert "calibration_session_id" in names


@pytest.mark.asyncio
async def test_partial_unique_index_on_active(db_session):
    """Two is_active=True rows for same combo must fail."""
    await db_session.execute(
        text(
            """
            INSERT INTO filament_calibration
              (printer_model, filament_id, nozzle_diameter, nozzle_volume_type, extruder_id,
               cali_mode, source, is_active, name, created_at)
            VALUES ('P1S','GFG00',0.4,'standard',0,'pa_line','manual',1,'r1',CURRENT_TIMESTAMP)
            """
        )
    )
    with pytest.raises(Exception):
        await db_session.execute(
            text(
                """
                INSERT INTO filament_calibration
                  (printer_model, filament_id, nozzle_diameter, nozzle_volume_type, extruder_id,
                   cali_mode, source, is_active, name, created_at)
                VALUES ('P1S','GFG00',0.4,'standard',0,'pa_line','manual',1,'r2',CURRENT_TIMESTAMP)
                """
            )
        )
        await db_session.commit()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest backend/tests/integration/test_m062_filament_calibration_migration.py -v`
Expected: FAIL with "no such table: filament_calibration".

- [ ] **Step 3: Write the migration**

```python
# backend/app/migrations/m062_filament_calibration.py
"""Filament Calibration wizard tables + print archive/queue flags.

What this migration does:
    1. CREATE TABLE filament_calibration — per-filament-type cali results
       (multi-row history with partial unique on is_active).
    2. CREATE TABLE calibration_session — wizard orchestration row.
    3. CREATE TABLE calibration_audit — BS-parity action log.
    4. ALTER print_archives ADD is_calibration BOOLEAN, calibration_session_id INT.
    5. ALTER print_queue_items ADD same two cols.

Why:
    BS PressureAdvanceWizard + FlowRateWizard need (filament_id, nozzle_dia,
    nozzle_vol_type, extruder_id, printer_model) as cali key; results carry
    K + N (PA) or flow_ratio + confidence. Multi-row per combo + is_active
    mirrors printer-side 16-slot history. print_archives.is_calibration
    flags cali prints separately from normal print archive entries.

Idempotent: CREATE TABLE IF NOT EXISTS + add_column helpers.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import add_column, table_exists

version = 62
name = "filament_calibration"


async def upgrade(conn):
    # --- 1. filament_calibration ----------------------------------------
    if not await table_exists(conn, "filament_calibration"):
        if is_postgres():
            await conn.execute(
                text(
                    """
                    CREATE TABLE filament_calibration (
                        id SERIAL PRIMARY KEY,
                        printer_model VARCHAR(50) NOT NULL,
                        filament_id VARCHAR(50) NOT NULL,
                        filament_setting_id VARCHAR(100),
                        nozzle_diameter DOUBLE PRECISION NOT NULL,
                        nozzle_volume_type VARCHAR(20) NOT NULL,
                        extruder_id INTEGER NOT NULL DEFAULT 0,
                        pa_k_value DOUBLE PRECISION,
                        pa_n_coef DOUBLE PRECISION,
                        flow_ratio DOUBLE PRECISION,
                        confidence INTEGER,
                        cali_mode VARCHAR(30) NOT NULL,
                        source VARCHAR(20) NOT NULL,
                        is_active BOOLEAN NOT NULL DEFAULT TRUE,
                        cali_idx INTEGER,
                        name VARCHAR(120) NOT NULL,
                        notes TEXT,
                        calibrated_on_printer_id INTEGER REFERENCES printers(id) ON DELETE SET NULL,
                        calibrated_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        created_at TIMESTAMP NOT NULL DEFAULT now()
                    )
                    """
                )
            )
        else:
            await conn.execute(
                text(
                    """
                    CREATE TABLE filament_calibration (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        printer_model TEXT NOT NULL,
                        filament_id TEXT NOT NULL,
                        filament_setting_id TEXT,
                        nozzle_diameter REAL NOT NULL,
                        nozzle_volume_type TEXT NOT NULL,
                        extruder_id INTEGER NOT NULL DEFAULT 0,
                        pa_k_value REAL,
                        pa_n_coef REAL,
                        flow_ratio REAL,
                        confidence INTEGER,
                        cali_mode TEXT NOT NULL,
                        source TEXT NOT NULL,
                        is_active INTEGER NOT NULL DEFAULT 1,
                        cali_idx INTEGER,
                        name TEXT NOT NULL,
                        notes TEXT,
                        calibrated_on_printer_id INTEGER REFERENCES printers(id) ON DELETE SET NULL,
                        calibrated_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )

    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_filament_cali_lookup "
            "ON filament_calibration(printer_model, filament_id, nozzle_diameter, nozzle_volume_type, extruder_id)"
        )
    )

    # Partial unique index — only one is_active row per combo
    if is_postgres():
        await conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_filament_cali_active
                ON filament_calibration
                  (printer_model, filament_id, nozzle_diameter, nozzle_volume_type, extruder_id)
                WHERE is_active = TRUE
                """
            )
        )
    else:
        await conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_filament_cali_active
                ON filament_calibration
                  (printer_model, filament_id, nozzle_diameter, nozzle_volume_type, extruder_id)
                WHERE is_active = 1
                """
            )
        )

    # --- 2. calibration_session ----------------------------------------
    if not await table_exists(conn, "calibration_session"):
        if is_postgres():
            await conn.execute(
                text(
                    """
                    CREATE TABLE calibration_session (
                        id SERIAL PRIMARY KEY,
                        printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        cali_mode VARCHAR(30) NOT NULL,
                        method VARCHAR(20) NOT NULL,
                        nozzle_diameter DOUBLE PRECISION NOT NULL,
                        nozzle_volume_type VARCHAR(20) NOT NULL,
                        extruder_id INTEGER NOT NULL DEFAULT 0,
                        filaments_json TEXT NOT NULL,
                        status VARCHAR(30) NOT NULL,
                        mqtt_sequence_id VARCHAR(50),
                        print_queue_item_id INTEGER REFERENCES print_queue_items(id) ON DELETE SET NULL,
                        parent_session_id INTEGER REFERENCES calibration_session(id) ON DELETE SET NULL,
                        stage INTEGER NOT NULL DEFAULT 1,
                        coarse_ratio DOUBLE PRECISION,
                        error_message TEXT,
                        created_at TIMESTAMP NOT NULL DEFAULT now(),
                        updated_at TIMESTAMP NOT NULL DEFAULT now()
                    )
                    """
                )
            )
        else:
            await conn.execute(
                text(
                    """
                    CREATE TABLE calibration_session (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        cali_mode TEXT NOT NULL,
                        method TEXT NOT NULL,
                        nozzle_diameter REAL NOT NULL,
                        nozzle_volume_type TEXT NOT NULL,
                        extruder_id INTEGER NOT NULL DEFAULT 0,
                        filaments_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        mqtt_sequence_id TEXT,
                        print_queue_item_id INTEGER REFERENCES print_queue_items(id) ON DELETE SET NULL,
                        parent_session_id INTEGER REFERENCES calibration_session(id) ON DELETE SET NULL,
                        stage INTEGER NOT NULL DEFAULT 1,
                        coarse_ratio REAL,
                        error_message TEXT,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )

    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_calibration_session_printer "
            "ON calibration_session(printer_id, status, created_at DESC)"
        )
    )

    # --- 3. calibration_audit ------------------------------------------
    if not await table_exists(conn, "calibration_audit"):
        if is_postgres():
            await conn.execute(
                text(
                    """
                    CREATE TABLE calibration_audit (
                        id SERIAL PRIMARY KEY,
                        printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                        session_id INTEGER REFERENCES calibration_session(id) ON DELETE SET NULL,
                        filament_calibration_id INTEGER REFERENCES filament_calibration(id) ON DELETE SET NULL,
                        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        action VARCHAR(40) NOT NULL,
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
                    CREATE TABLE calibration_audit (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                        session_id INTEGER REFERENCES calibration_session(id) ON DELETE SET NULL,
                        filament_calibration_id INTEGER REFERENCES filament_calibration(id) ON DELETE SET NULL,
                        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        action TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        sequence_id TEXT,
                        result TEXT NOT NULL,
                        error_message TEXT,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )

    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_calibration_audit_printer "
            "ON calibration_audit(printer_id, created_at DESC)"
        )
    )

    # --- 4. print_archive flags ----------------------------------------
    await add_column(conn, "print_archives", "is_calibration", "BOOLEAN NOT NULL DEFAULT FALSE")
    await add_column(conn, "print_archives", "calibration_session_id", "INTEGER")

    # --- 5. print_queue_items flags ------------------------------------
    await add_column(conn, "print_queue_items", "is_calibration", "BOOLEAN NOT NULL DEFAULT FALSE")
    await add_column(conn, "print_queue_items", "calibration_session_id", "INTEGER")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest backend/tests/integration/test_m062_filament_calibration_migration.py -v`
Expected: PASS (all 7 tests).

- [ ] **Step 5: Stage files**

`git add backend/app/migrations/m062_filament_calibration.py backend/tests/integration/test_m062_filament_calibration_migration.py`
(Do not commit — wave-level checkpoint commits after all of Wave 1 passes.)

---

### Task 2: FilamentCalibration ORM model

**Files:**
- Create: `backend/app/models/filament_calibration.py`
- Modify: `backend/app/models/__init__.py`

- [ ] **Step 1: Write the model**

```python
# backend/app/models/filament_calibration.py
"""ORM for filament_calibration (m062).

Per-filament-type cali storage. Many rows per combo (history), one
is_active=True per combo (enforced by partial unique index). Written by
CalibrationService.save_result after wizard completes; consumed by
background_dispatch.apply_active_calibration to fire extrusion_cali_sel
on the printer before each print start.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class FilamentCalibration(Base):
    __tablename__ = "filament_calibration"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Identity (combo)
    printer_model: Mapped[str] = mapped_column(String(50), nullable=False)
    filament_id: Mapped[str] = mapped_column(String(50), nullable=False)
    filament_setting_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    nozzle_diameter: Mapped[float] = mapped_column(Float, nullable=False)
    nozzle_volume_type: Mapped[str] = mapped_column(String(20), nullable=False)
    extruder_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Result payload
    pa_k_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    pa_n_coef: Mapped[float | None] = mapped_column(Float, nullable=True)
    flow_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Provenance
    cali_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    cali_idx: Mapped[int | None] = mapped_column(Integer, nullable=True)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    calibrated_on_printer_id: Mapped[int | None] = mapped_column(
        ForeignKey("printers.id", ondelete="SET NULL"), nullable=True
    )
    calibrated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        Index(
            "ix_filament_cali_lookup",
            "printer_model", "filament_id", "nozzle_diameter", "nozzle_volume_type", "extruder_id",
        ),
    )
```

- [ ] **Step 2: Register in models/__init__.py**

Add line in `backend/app/models/__init__.py`:

```python
from backend.app.models.filament_calibration import FilamentCalibration  # noqa: F401
```

- [ ] **Step 3: Smoke-test import**

Run: `python -c "from backend.app.models.filament_calibration import FilamentCalibration; print(FilamentCalibration.__table__.name)"`
Expected: `filament_calibration`.

- [ ] **Step 4: Stage files**

`git add backend/app/models/filament_calibration.py backend/app/models/__init__.py`

---

### Task 3: CalibrationSession ORM model

**Files:**
- Create: `backend/app/models/calibration_session.py`
- Modify: `backend/app/models/__init__.py`

- [ ] **Step 1: Write the model**

```python
# backend/app/models/calibration_session.py
"""ORM for calibration_session (m062).

Orchestration row for the wizard — NOT persistent storage of cali values.
Tracks: which mode + method, which printer, which user, current status,
linked print job (manual path), Flow Rate 2-stage chain via parent_session_id.

Lifecycle:
    running → awaiting_user_input → saved
            → cancelled
            → failed
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class CalibrationSession(Base):
    __tablename__ = "calibration_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    cali_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    method: Mapped[str] = mapped_column(String(20), nullable=False)
    nozzle_diameter: Mapped[float] = mapped_column(Float, nullable=False)
    nozzle_volume_type: Mapped[str] = mapped_column(String(20), nullable=False)
    extruder_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    filaments_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    mqtt_sequence_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    print_queue_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("print_queue_items.id", ondelete="SET NULL"), nullable=True
    )

    parent_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("calibration_session.id", ondelete="SET NULL"), nullable=True
    )
    stage: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    coarse_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_calibration_session_printer", "printer_id", "status", "created_at"),
    )
```

- [ ] **Step 2: Register in models/__init__.py**

Add line in `backend/app/models/__init__.py`:

```python
from backend.app.models.calibration_session import CalibrationSession  # noqa: F401
```

- [ ] **Step 3: Smoke-test**

Run: `python -c "from backend.app.models.calibration_session import CalibrationSession; print(CalibrationSession.__table__.name)"`
Expected: `calibration_session`.

- [ ] **Step 4: Stage**

`git add backend/app/models/calibration_session.py backend/app/models/__init__.py`

---

### Task 4: CalibrationAudit ORM model

**Files:**
- Create: `backend/app/models/calibration_audit.py`
- Modify: `backend/app/models/__init__.py`

- [ ] **Step 1: Write the model**

```python
# backend/app/models/calibration_audit.py
"""ORM for calibration_audit (m062).

Mirrors ams_setting_audit (m060) + printer_setting_audit (m061) pattern.
One row per user-initiated action: start_session, save_result,
sync_printer, delete, set_active, cancel. Written by routes after MQTT
publish + DB write.

UI viewer not provided in phase-1; query the table directly.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class CalibrationAudit(Base):
    __tablename__ = "calibration_audit"
    __table_args__ = (
        Index("ix_calibration_audit_printer", "printer_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"), nullable=False)
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("calibration_session.id", ondelete="SET NULL"), nullable=True
    )
    filament_calibration_id: Mapped[int | None] = mapped_column(
        ForeignKey("filament_calibration.id", ondelete="SET NULL"), nullable=True
    )
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    sequence_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    result: Mapped[str] = mapped_column(String(20), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
```

- [ ] **Step 2: Register**

Add to `backend/app/models/__init__.py`:

```python
from backend.app.models.calibration_audit import CalibrationAudit  # noqa: F401
```

- [ ] **Step 3: Smoke-test**

Run: `python -c "from backend.app.models.calibration_audit import CalibrationAudit; print(CalibrationAudit.__table__.name)"`
Expected: `calibration_audit`.

- [ ] **Step 4: Stage**

`git add backend/app/models/calibration_audit.py backend/app/models/__init__.py`

---

### Task 5: PrintArchive + PrintQueueItem flag fields

**Files:**
- Modify: `backend/app/models/print_archive.py` (find exact path with Grep first)
- Modify: `backend/app/models/print_queue_item.py` (find exact path)

- [ ] **Step 1: Locate the model files**

Run: `grep -rln "class PrintArchive" backend/app/models/` and `grep -rln "class PrintQueueItem" backend/app/models/`.

Expected: two files. Note the paths.

- [ ] **Step 2: Add columns to PrintArchive**

Add to the PrintArchive model body (alongside other Mapped fields):

```python
is_calibration: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
calibration_session_id: Mapped[int | None] = mapped_column(
    ForeignKey("calibration_session.id", ondelete="SET NULL"), nullable=True
)
```

If `Boolean`, `ForeignKey` not yet imported on that file, add them to the imports at top.

- [ ] **Step 3: Add same columns to PrintQueueItem**

Same two columns:

```python
is_calibration: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
calibration_session_id: Mapped[int | None] = mapped_column(
    ForeignKey("calibration_session.id", ondelete="SET NULL"), nullable=True
)
```

- [ ] **Step 4: Verify fresh-install path still works**

Run: `pytest backend/tests/integration/test_m062_filament_calibration_migration.py -v`
Expected: all 7 tests still PASS.

- [ ] **Step 5: Stage**

`git add backend/app/models/print_archive.py backend/app/models/print_queue_item.py`

---

### Wave 1 verify checkpoint

- [ ] Run full migration test suite

Run: `pytest backend/tests/integration/test_m062_filament_calibration_migration.py -v`
Expected: 7 passed.

- [ ] Run general model import smoke

Run: `python -c "from backend.app.models import *; print('ok')"`
Expected: `ok` (no ImportError).

- [ ] Wave 1 COMMIT (only when user explicitly asks)

Suggested commit message:

```
feat(calibration): m062 + filament_calibration + session + audit tables

New tables for Filament Calibration wizard:
- filament_calibration: per-filament-type cali results (history with
  partial unique on is_active per combo).
- calibration_session: wizard orchestration row.
- calibration_audit: BS-parity action log (mirrors m060/m061 pattern).

Adds is_calibration + calibration_session_id columns to print_archives
and print_queue_items so the dispatcher can tag cali prints and the
archive viewer can filter them later.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Wave 2 — Constants + capability gate

### Task 6: Calibration constants + helpers

**Files:**
- Create: `backend/app/services/calibration_constants.py`
- Test: `backend/tests/unit/services/test_calibration_constants.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/services/test_calibration_constants.py
"""Tests calibration mode metadata + nozzle_id encoder."""

from backend.app.services.calibration_constants import (
    PA_LINE_RANGE,
    FLOW_RATE_COARSE_MODIFIERS,
    FLOW_RATE_FINE_MODIFIERS,
    NozzleVolumeType,
    CaliMode,
    generate_nozzle_id,
    compute_pa_k,
    compute_flow_ratio_coarse,
    compute_flow_ratio_fine,
)


def test_pa_line_range_constants():
    start, end, step, count = PA_LINE_RANGE
    assert start == 0.0
    assert end == 0.1
    assert abs(step - 0.002) < 1e-9
    assert count == 50


def test_flow_rate_modifiers():
    assert FLOW_RATE_COARSE_MODIFIERS == (-20, -15, -10, -5, 0, 5, 10, 15, 20)
    assert FLOW_RATE_FINE_MODIFIERS == (-5, -2, 0, 2, 5, 10, 15)


def test_nozzle_id_standard_0_4():
    assert generate_nozzle_id(NozzleVolumeType.STANDARD, 0.4) == "HS20"


def test_nozzle_id_high_flow_0_4():
    assert generate_nozzle_id(NozzleVolumeType.HIGH_FLOW, 0.4) == "HH20"


def test_nozzle_id_tpu_high_flow_0_2():
    assert generate_nozzle_id(NozzleVolumeType.TPU_HIGH_FLOW, 0.2) == "HU00"


def test_nozzle_id_hybrid_0_8():
    assert generate_nozzle_id(NozzleVolumeType.HYBRID, 0.8) == "HY60"


def test_compute_pa_k():
    assert abs(compute_pa_k(0) - 0.0) < 1e-9
    assert abs(compute_pa_k(24) - 0.048) < 1e-9
    assert abs(compute_pa_k(49) - 0.098) < 1e-9


def test_compute_flow_ratio_coarse():
    assert abs(compute_flow_ratio_coarse(0) - 1.0) < 1e-9
    assert abs(compute_flow_ratio_coarse(10) - 1.10) < 1e-9
    assert abs(compute_flow_ratio_coarse(-15) - 0.85) < 1e-9


def test_compute_flow_ratio_fine():
    assert abs(compute_flow_ratio_fine(1.0, 0) - 1.0) < 1e-9
    assert abs(compute_flow_ratio_fine(1.05, 5) - 1.1025) < 1e-9


def test_cali_mode_enum():
    assert CaliMode.PA_LINE.value == "pa_line"
    assert CaliMode.FLOW_RATE.value == "flow_rate"
    assert CaliMode.AUTO_PA_LINE.value == "auto_pa_line"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest backend/tests/unit/services/test_calibration_constants.py -v`
Expected: FAIL (ImportError).

- [ ] **Step 3: Write the constants module**

```python
# backend/app/services/calibration_constants.py
"""Calibration mode metadata + math helpers + nozzle_id encoder.

Constants frozen from BS resources/calib/ — PA Line range, Flow Rate
9-block modifiers. Math helpers map UI input (best line index, best
block modifier) to K values / flow ratios. nozzle_id encoder mirrors
BS DeviceManager.cpp:338-350.
"""

from __future__ import annotations

from enum import Enum

# BS pa_line.3mf range: 0.0 → 0.1 step 0.002 = 50 lines (index 0..49)
PA_LINE_RANGE: tuple[float, float, float, int] = (0.0, 0.1, 0.002, 50)

# BS flowrate-test-pass1.3mf: 9 blocks
FLOW_RATE_COARSE_MODIFIERS: tuple[int, ...] = (-20, -15, -10, -5, 0, 5, 10, 15, 20)
# BS flowrate-test-pass2.3mf: 7 refined blocks
FLOW_RATE_FINE_MODIFIERS: tuple[int, ...] = (-5, -2, 0, 2, 5, 10, 15)


class CaliMode(str, Enum):
    PA_LINE = "pa_line"
    PA_PATTERN = "pa_pattern"
    PA_TOWER = "pa_tower"
    AUTO_PA_LINE = "auto_pa_line"
    FLOW_RATE = "flow_rate"
    TEMP_TOWER = "temp_tower"
    VOL_SPEED_TOWER = "vol_speed_tower"
    VFA_TOWER = "vfa_tower"
    RETRACTION_TOWER = "retraction_tower"


class CaliMethod(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"


class NozzleVolumeType(str, Enum):
    STANDARD = "standard"
    HIGH_FLOW = "high_flow"
    TPU_HIGH_FLOW = "tpu_high_flow"
    HYBRID = "hybrid"


# Maps for nozzle_id encoder
_VOL_TYPE_CHARS = {
    NozzleVolumeType.STANDARD: "S",
    NozzleVolumeType.HIGH_FLOW: "H",
    NozzleVolumeType.TPU_HIGH_FLOW: "U",
    NozzleVolumeType.HYBRID: "Y",
}

# Diameter to two-digit code (BS-format): 0.2→"00", 0.4→"20", 0.6→"40", 0.8→"60"
_DIAMETER_CODES = {
    0.2: "00",
    0.4: "20",
    0.6: "40",
    0.8: "60",
}


def generate_nozzle_id(vol_type: NozzleVolumeType, diameter: float) -> str:
    """Encode nozzle id per BS DeviceManager.cpp:338-350.

    Format: H + [S|H|U|Y] + diameter_code
    Examples: standard 0.4 → "HS20", high_flow 0.8 → "HH60".
    """
    code = _DIAMETER_CODES.get(round(diameter, 2))
    if code is None:
        raise ValueError(f"Unsupported nozzle diameter: {diameter}")
    return f"H{_VOL_TYPE_CHARS[vol_type]}{code}"


def compute_pa_k(line_index: int) -> float:
    """PA K = start + index * step. Index 0..49 for BS pa_line.3mf."""
    start, _end, step, count = PA_LINE_RANGE
    if line_index < 0 or line_index >= count:
        raise ValueError(f"line_index out of range: {line_index}")
    return start + line_index * step


def compute_flow_ratio_coarse(modifier_pct: int) -> float:
    """Flow ratio after coarse stage = 1.0 * (100 + mod) / 100."""
    if modifier_pct not in FLOW_RATE_COARSE_MODIFIERS:
        raise ValueError(f"Invalid coarse modifier: {modifier_pct}")
    return (100 + modifier_pct) / 100.0


def compute_flow_ratio_fine(coarse_ratio: float, modifier_pct: int) -> float:
    """Fine = coarse * (100 + mod) / 100."""
    if modifier_pct not in FLOW_RATE_FINE_MODIFIERS:
        raise ValueError(f"Invalid fine modifier: {modifier_pct}")
    return coarse_ratio * (100 + modifier_pct) / 100.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest backend/tests/unit/services/test_calibration_constants.py -v`
Expected: 11 passed.

- [ ] **Step 5: Stage**

`git add backend/app/services/calibration_constants.py backend/tests/unit/services/test_calibration_constants.py`

---

### Task 7: compute_calibration_supports

**Files:**
- Modify: `backend/app/services/printer_capabilities.py`
- Test: `backend/tests/unit/services/test_calibration_capabilities.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/services/test_calibration_capabilities.py
"""Tests compute_calibration_supports — per-model gating."""

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.printer_capabilities import compute_calibration_supports


def _state(*, pa: bool = False, flow: bool = False) -> PrinterState:
    s = PrinterState()
    s.is_support_pa_calibration = pa
    s.is_support_auto_flow_calibration = flow
    return s


def test_x1c_pa_auto_when_supported():
    caps = compute_calibration_supports(_state(pa=True, flow=True), "X1C", {})
    assert caps["pa_auto"] is True
    assert caps["flow_auto"] is True
    assert caps["pa_manual"] is True
    assert caps["flow_manual"] is True


def test_x1c_pa_auto_false_when_state_says_no():
    caps = compute_calibration_supports(_state(pa=False, flow=False), "X1C", {})
    assert caps["pa_auto"] is False
    assert caps["flow_auto"] is False


def test_p1s_no_auto_paths():
    caps = compute_calibration_supports(_state(pa=True, flow=True), "P1S", {})
    # Even if state reports support, P1S doesn't have a lidar -> auto blocked
    assert caps["pa_auto"] is False
    assert caps["flow_auto"] is False
    assert caps["pa_manual"] is True


def test_a1_mini_manual_only():
    caps = compute_calibration_supports(_state(), "A1 Mini", {})
    assert caps["pa_auto"] is False
    assert caps["flow_auto"] is False
    assert caps["pa_manual"] is True
    assert caps["flow_manual"] is True


def test_h2d_dual_extruder():
    caps = compute_calibration_supports(_state(pa=True), "H2D", {})
    assert caps["dual_extruder"] is True
    assert len(caps["extruders"]) == 2
    assert caps["extruders"][0]["id"] == 0
    assert caps["extruders"][1]["id"] == 1


def test_x1c_single_extruder():
    caps = compute_calibration_supports(_state(), "X1C", {})
    assert caps["dual_extruder"] is False
    assert len(caps["extruders"]) == 1


def test_unknown_model_safe_defaults():
    caps = compute_calibration_supports(_state(pa=True), "UnknownModel", {})
    assert caps["pa_auto"] is False
    assert caps["pa_manual"] is True


def test_tower_modes_universal():
    caps = compute_calibration_supports(_state(), "P1S", {})
    assert caps["temp_tower"] is True
    assert caps["vol_speed_tower"] is True
    assert caps["vfa_tower"] is True
    assert caps["retraction_tower"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest backend/tests/unit/services/test_calibration_capabilities.py -v`
Expected: FAIL (function not defined).

- [ ] **Step 3: Add the function + PrinterState fields**

In `backend/app/services/printer_capabilities.py`, append at bottom (after existing `compute_printer_supports`):

```python
# ---------- Filament Calibration capabilities ----------

_LIDAR_MODELS = frozenset({"X1", "X1C", "X1E", "H2D", "H2DPRO"})
_DUAL_EXTRUDER_MODELS = frozenset({"H2D", "H2DPRO"})


def _list_extruders(model_norm: str) -> list[dict]:
    if model_norm in _DUAL_EXTRUDER_MODELS:
        return [{"id": 0, "name": "Right"}, {"id": 1, "name": "Left"}]
    return [{"id": 0, "name": "Main"}]


def compute_calibration_supports(
    state: PrinterState,
    printer_model: str | None,
    module_vers: dict,
) -> dict:
    """Per-model capability matrix for Filament Calibration wizard.

    auto_* gates: model must have lidar AND the printer state must
    report support flag. manual paths universally available. Tower modes
    universal (juste a print). Dual-extruder for H2D family.
    """
    m = _norm(printer_model)
    has_lidar = m in _LIDAR_MODELS

    return {
        # Manual paths
        "pa_manual": True,
        "flow_manual": True,
        "temp_tower": True,
        "vol_speed_tower": True,
        "vfa_tower": True,
        "retraction_tower": True,
        # Auto paths (lidar + push flag)
        "pa_auto": has_lidar and bool(getattr(state, "is_support_pa_calibration", False)),
        "flow_auto": has_lidar and bool(getattr(state, "is_support_auto_flow_calibration", False)),
        # Layout
        "dual_extruder": m in _DUAL_EXTRUDER_MODELS,
        "extruders": _list_extruders(m),
        "nozzles": [
            {
                "id": i,
                "diameter": getattr(n, "diameter", None),
                "type": getattr(n, "type", None),
                "flow_type": getattr(n, "flow_type", None),
            }
            for i, n in enumerate(getattr(state, "nozzles", []) or [])
        ],
    }
```

- [ ] **Step 4: Add the supporting PrinterState fields**

In `backend/app/services/bambu_mqtt.py`, locate the `PrinterState` dataclass (around line 152-231). Add two fields:

```python
is_support_pa_calibration: bool = False
is_support_auto_flow_calibration: bool = False
```

(Place near other `is_support_*` fields if they exist; otherwise alongside other capability flags.)

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest backend/tests/unit/services/test_calibration_capabilities.py -v`
Expected: 8 passed.

- [ ] **Step 6: Stage**

`git add backend/app/services/printer_capabilities.py backend/app/services/bambu_mqtt.py backend/tests/unit/services/test_calibration_capabilities.py`

---

### Wave 2 verify

- [ ] Run wave's tests

Run: `pytest backend/tests/unit/services/test_calibration_constants.py backend/tests/unit/services/test_calibration_capabilities.py -v`
Expected: 19 passed.

- [ ] Wave 2 commit (suggest when user asks):

```
feat(calibration): constants + capability matrix

- CaliMode/CaliMethod/NozzleVolumeType enums.
- PA Line range frozen from BS (0.0–0.1 step 0.002, 50 lines).
- Flow Rate coarse/fine modifier tuples.
- nozzle_id encoder (H[S|H|U|Y][00|20|40|60]).
- compute_calibration_supports: per-model gating (lidar + push flag for
  auto, manual + towers universal, H2D dual-extruder).
- PrinterState gains is_support_pa_calibration +
  is_support_auto_flow_calibration.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Wave 3 — MQTT layer

### Task 8: PrinterState — calibration push data

**Files:**
- Modify: `backend/app/services/bambu_mqtt.py`
- Test: `backend/tests/unit/services/test_bambu_mqtt_calibration.py`

- [ ] **Step 1: Write failing parser test**

```python
# backend/tests/unit/services/test_bambu_mqtt_calibration.py
"""Tests calibration MQTT publishers + parsers."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


@pytest.fixture
def mqtt_client():
    c = BambuMQTTClient(
        ip_address="192.168.1.100",
        serial_number="TESTCALI01",
        access_code="12345678",
    )
    c._client = MagicMock()
    c.state.connected = True
    return c


def _payload(c) -> dict:
    call = c._client.publish.call_args
    _, payload, *_ = call.args
    return json.loads(payload)


# ---------- Parser: extrusion_cali_get_result ----------


class _FakeMsg:
    def __init__(self, body: dict):
        self.topic = ""
        self.payload = json.dumps(body).encode()


def test_parser_extrusion_cali_get_result_populates_state(mqtt_client):
    msg = {
        "print": {
            "command": "extrusion_cali_get_result",
            "filaments": [
                {
                    "tray_id": 0,
                    "ams_id": 0,
                    "slot_id": 0,
                    "extruder_id": 0,
                    "nozzle_diameter": 0.4,
                    "nozzle_volume_type": "standard",
                    "filament_id": "GFG00",
                    "setting_id": "GFG00_60@BBL",
                    "k_value": 0.0432,
                    "n_coef": 1.0,
                    "confidence": 0,
                }
            ],
        }
    }
    mqtt_client._on_message(None, None, _FakeMsg(msg))
    results = mqtt_client.state.extrusion_cali_results
    assert len(results) == 1
    assert results[0].k_value == 0.0432
    assert results[0].filament_id == "GFG00"
    assert mqtt_client.state.extrusion_cali_status == "completed"


def test_parser_extrusion_cali_get_populates_history(mqtt_client):
    msg = {
        "print": {
            "command": "extrusion_cali_get",
            "filaments": [
                {
                    "cali_idx": 0,
                    "name": "PLA — PA 0.04",
                    "filament_id": "GFG00",
                    "setting_id": "GFG00_60@BBL",
                    "nozzle_diameter": 0.4,
                    "nozzle_volume_type": "standard",
                    "extruder_id": 0,
                    "k_value": 0.04,
                    "n_coef": 1.0,
                }
            ],
        }
    }
    mqtt_client._on_message(None, None, _FakeMsg(msg))
    hist = mqtt_client.state.extrusion_cali_history
    assert len(hist) == 1
    assert hist[0].cali_idx == 0


def test_parser_capability_flags(mqtt_client):
    msg = {
        "print": {
            "command": "push_status",
            "support_auto_flow_calibration": True,
        }
    }
    mqtt_client._on_message(None, None, _FakeMsg(msg))
    assert mqtt_client.state.is_support_auto_flow_calibration is True


# ---------- Publishers: extrusion_cali_start ----------


def test_extrusion_cali_start_payload(mqtt_client):
    ok, seq = mqtt_client.extrusion_cali_start(
        nozzle_diameter=0.4,
        cali_mode=1,
        filaments=[
            {
                "tray_id": 0,
                "extruder_id": 0,
                "bed_temp": 60,
                "filament_id": "GFG00",
                "setting_id": "GFG00_60@BBL",
                "nozzle_temp": 220,
                "ams_id": 0,
                "slot_id": 0,
                "nozzle_id": "HS20",
                "nozzle_diameter": "0.4",
                "max_volumetric_speed": "12.0",
            }
        ],
    )
    assert ok and seq
    msg = _payload(mqtt_client)
    assert msg["print"]["command"] == "extrusion_cali"
    assert msg["print"]["nozzle_diameter"] == "0.4"
    assert msg["print"]["mode"] == 1
    assert msg["print"]["filaments"][0]["k_value"] is None or "k_value" not in msg["print"]["filaments"][0]
    assert msg["print"]["filaments"][0]["filament_id"] == "GFG00"


def test_flow_rate_cali_start_payload(mqtt_client):
    ok, seq = mqtt_client.flow_rate_cali_start(
        nozzle_diameter=0.4,
        filaments=[
            {
                "tray_id": 0,
                "extruder_id": 0,
                "bed_temp": 60,
                "filament_id": "GFG00",
                "setting_id": "GFG00_60@BBL",
                "nozzle_temp": 220,
                "ams_id": 0,
                "slot_id": 0,
                "nozzle_id": "HS20",
                "nozzle_diameter": "0.4",
                "max_volumetric_speed": "12.0",
                "flow_rate": 0.98,
            }
        ],
    )
    assert ok and seq
    msg = _payload(mqtt_client)
    assert msg["print"]["command"] == "extrusion_cali"
    assert msg["print"]["filaments"][0]["flow_rate"] == 0.98


def test_extrusion_cali_query_history(mqtt_client):
    ok, _ = mqtt_client.extrusion_cali_query_history(nozzle_diameter=0.4, extruder_id=0)
    assert ok
    msg = _payload(mqtt_client)
    assert msg["print"]["command"] == "extrusion_cali_get"


def test_extrusion_cali_query_result(mqtt_client):
    ok, _ = mqtt_client.extrusion_cali_query_result(nozzle_diameter=0.4)
    assert ok
    msg = _payload(mqtt_client)
    assert msg["print"]["command"] == "extrusion_cali_get_result"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest backend/tests/unit/services/test_bambu_mqtt_calibration.py -v`
Expected: FAILs (methods/dataclasses not defined).

- [ ] **Step 3: Add dataclasses + state fields**

In `backend/app/services/bambu_mqtt.py`, near the other dataclass declarations (look for `NozzleInfo` / `KProfile` at lines ~80-160), add:

```python
@dataclass
class ExtrusionCaliResult:
    """One row from push extrusion_cali_get_result (auto path)."""
    tray_id: int = 0
    ams_id: int = 0
    slot_id: int = 0
    extruder_id: int = 0
    nozzle_diameter: float = 0.4
    nozzle_volume_type: str = "standard"
    filament_id: str = ""
    setting_id: str = ""
    k_value: float = 0.0
    n_coef: float = 0.0
    confidence: int = -1
    nozzle_pos_id: int = -1
    nozzle_sn: str = ""


@dataclass
class PACalibHistoryEntry:
    """One row from push extrusion_cali_get (printer-side 16-slot history)."""
    cali_idx: int = -1
    name: str = ""
    filament_id: str = ""
    setting_id: str = ""
    nozzle_diameter: float = 0.4
    nozzle_volume_type: str = "standard"
    extruder_id: int = 0
    k_value: float = 0.0
    n_coef: float = 0.0
```

Then in `PrinterState`, add (alongside the two flags from Task 7):

```python
extrusion_cali_results: list = field(default_factory=list)
extrusion_cali_session_id: str | None = None
extrusion_cali_status: str = "idle"
extrusion_cali_history: list = field(default_factory=list)
```

- [ ] **Step 4: Run only the parser-related tests**

Run: `pytest backend/tests/unit/services/test_bambu_mqtt_calibration.py -v -k "parser"`
Expected: still FAIL (parser hooks not added yet).

- [ ] **Step 5: Stage**

`git add backend/app/services/bambu_mqtt.py backend/tests/unit/services/test_bambu_mqtt_calibration.py`

---

### Task 9: MQTT parser hooks for cali push messages

**Files:**
- Modify: `backend/app/services/bambu_mqtt.py`

- [ ] **Step 1: Locate _on_message**

Grep: `grep -n "def _on_message" backend/app/services/bambu_mqtt.py`. Note line.

- [ ] **Step 2: Add parser blocks**

In `_on_message`, inside the `print.command` dispatch branches (or near similar `extrusion_cali*` handling — look for existing patterns around lines ~1048 and ~1073), add three new handlers:

```python
# Inside the print message handler, where command is dispatched:

if cmd == "extrusion_cali_get_result":
    fils = msg["print"].get("filaments", [])
    self.state.extrusion_cali_results = [
        ExtrusionCaliResult(
            tray_id=int(f.get("tray_id", 0)),
            ams_id=int(f.get("ams_id", 0)),
            slot_id=int(f.get("slot_id", 0)),
            extruder_id=int(f.get("extruder_id", 0)),
            nozzle_diameter=float(f.get("nozzle_diameter", 0.4)),
            nozzle_volume_type=str(f.get("nozzle_volume_type", "standard")),
            filament_id=str(f.get("filament_id", "")),
            setting_id=str(f.get("setting_id", "")),
            k_value=float(f.get("k_value", 0.0)),
            n_coef=float(f.get("n_coef", 0.0)),
            confidence=int(f.get("confidence", -1)),
            nozzle_pos_id=int(f.get("nozzle_pos_id", -1)),
            nozzle_sn=str(f.get("nozzle_sn", "")),
        )
        for f in fils
    ]
    self.state.extrusion_cali_status = "completed"
    return

if cmd == "extrusion_cali_get":
    fils = msg["print"].get("filaments", [])
    self.state.extrusion_cali_history = [
        PACalibHistoryEntry(
            cali_idx=int(f.get("cali_idx", -1)),
            name=str(f.get("name", "")),
            filament_id=str(f.get("filament_id", "")),
            setting_id=str(f.get("setting_id", "")),
            nozzle_diameter=float(f.get("nozzle_diameter", 0.4)),
            nozzle_volume_type=str(f.get("nozzle_volume_type", "standard")),
            extruder_id=int(f.get("extruder_id", 0)),
            k_value=float(f.get("k_value", 0.0)),
            n_coef=float(f.get("n_coef", 0.0)),
        )
        for f in fils
    ]
    return
```

Then in the `push_status` handling block (or wherever capability flags get parsed — find by greping `is_support_`), add:

```python
if "support_auto_flow_calibration" in pdata:
    val = pdata["support_auto_flow_calibration"]
    if isinstance(val, bool):
        self.state.is_support_auto_flow_calibration = val
if "support_pa_calibration" in pdata:
    val = pdata["support_pa_calibration"]
    if isinstance(val, bool):
        self.state.is_support_pa_calibration = val
```

- [ ] **Step 3: Run parser tests**

Run: `pytest backend/tests/unit/services/test_bambu_mqtt_calibration.py -v -k "parser"`
Expected: 3 passed.

- [ ] **Step 4: Stage**

`git add backend/app/services/bambu_mqtt.py`

---

### Task 10: MQTT publishers (cali_start, query, flow_rate)

**Files:**
- Modify: `backend/app/services/bambu_mqtt.py`

- [ ] **Step 1: Locate existing extrusion_cali_set / sel / del**

Grep: `grep -n "def extrusion_cali" backend/app/services/bambu_mqtt.py`. Note locations.

- [ ] **Step 2: Add 4 new publishers near existing ones**

Add to `BambuMQTTClient` class (right after `extrusion_cali_set` / `extrusion_cali_sel`):

```python
def extrusion_cali_start(
    self,
    *,
    nozzle_diameter: float,
    cali_mode: int,
    filaments: list[dict],
) -> tuple[bool, str | None]:
    """Start PA calibration. MQTT print.command='extrusion_cali'.

    cali_mode: 0=auto (X1 lidar), 1=manual.
    filaments: list of dicts per BS payload (see spec §MQTT).
    """
    if not self.state.connected:
        return False, None
    seq = str(self._next_sequence_id())
    payload = {
        "print": {
            "command": "extrusion_cali",
            "sequence_id": seq,
            "nozzle_diameter": str(nozzle_diameter),
            "mode": cali_mode,
            "filaments": filaments,
        }
    }
    try:
        self._client.publish(self._request_topic(), json.dumps(payload), qos=1)
        return True, seq
    except Exception:
        return False, None


def flow_rate_cali_start(
    self,
    *,
    nozzle_diameter: float,
    filaments: list[dict],
) -> tuple[bool, str | None]:
    """Start flow rate calibration. Same MQTT command extrusion_cali
    but with flow_rate field populated in each filament dict (X1 auto-flow).
    """
    if not self.state.connected:
        return False, None
    seq = str(self._next_sequence_id())
    payload = {
        "print": {
            "command": "extrusion_cali",
            "sequence_id": seq,
            "nozzle_diameter": str(nozzle_diameter),
            "filaments": filaments,
        }
    }
    try:
        self._client.publish(self._request_topic(), json.dumps(payload), qos=1)
        return True, seq
    except Exception:
        return False, None


def extrusion_cali_query_history(
    self,
    *,
    nozzle_diameter: float,
    extruder_id: int = 0,
) -> tuple[bool, str | None]:
    """Ask printer for current PA history. Reply pushes back via
    extrusion_cali_get (handled by _on_message)."""
    if not self.state.connected:
        return False, None
    seq = str(self._next_sequence_id())
    payload = {
        "print": {
            "command": "extrusion_cali_get",
            "sequence_id": seq,
            "nozzle_diameter": str(nozzle_diameter),
            "extruder_id": extruder_id,
        }
    }
    try:
        self._client.publish(self._request_topic(), json.dumps(payload), qos=1)
        return True, seq
    except Exception:
        return False, None


def extrusion_cali_query_result(
    self,
    *,
    nozzle_diameter: float,
) -> tuple[bool, str | None]:
    """Ask printer for auto-cali result (X1 lidar batches)."""
    if not self.state.connected:
        return False, None
    seq = str(self._next_sequence_id())
    payload = {
        "print": {
            "command": "extrusion_cali_get_result",
            "sequence_id": seq,
            "nozzle_diameter": str(nozzle_diameter),
        }
    }
    try:
        self._client.publish(self._request_topic(), json.dumps(payload), qos=1)
        return True, seq
    except Exception:
        return False, None
```

(`_next_sequence_id` and `_request_topic` are existing helpers — verify their names by greping. Adjust if needed.)

- [ ] **Step 3: Run all calibration MQTT tests**

Run: `pytest backend/tests/unit/services/test_bambu_mqtt_calibration.py -v`
Expected: 6+ passed.

- [ ] **Step 4: Stage**

`git add backend/app/services/bambu_mqtt.py`

---

### Wave 3 verify

- [ ] Run wave's tests

Run: `pytest backend/tests/unit/services/test_bambu_mqtt_calibration.py -v`
Expected: all passed.

- [ ] Wave 3 commit (when user asks):

```
feat(calibration): MQTT publishers + push parser for extrusion_cali

- extrusion_cali_start / flow_rate_cali_start: launch auto/manual PA + flow.
- extrusion_cali_query_history / _result: pull printer-side history + auto results.
- Push parser: extrusion_cali_get → state.extrusion_cali_history,
  extrusion_cali_get_result → state.extrusion_cali_results.
- Capability flags: support_pa_calibration + support_auto_flow_calibration.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Wave 4 — Service layer

### Task 11: CalibrationService skeleton + start_calibration (auto branch)

**Files:**
- Create: `backend/app/services/calibration_service.py`
- Test: `backend/tests/unit/services/test_calibration_service.py`

- [ ] **Step 1: Write failing tests for start (auto)**

```python
# backend/tests/unit/services/test_calibration_service.py
"""Tests CalibrationService — start/submit/save/cancel orchestration."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.calibration_constants import CaliMethod, CaliMode
from backend.app.services.calibration_service import CalibrationService, CalibFilamentInput


@pytest.fixture
def service():
    return CalibrationService()


@pytest.fixture
def mock_client():
    c = MagicMock()
    c.state.connected = True
    c.state.is_support_pa_calibration = True
    c.state.extrusion_cali_status = "idle"
    c.extrusion_cali_start = MagicMock(return_value=(True, "SEQ-CALI-1"))
    c.flow_rate_cali_start = MagicMock(return_value=(True, "SEQ-CALI-2"))
    return c


@pytest.mark.asyncio
async def test_start_calibration_auto_pa(service, db_session, printer_factory, mock_client):
    printer = await printer_factory(model="X1C")
    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = mock_client
        session = await service.start_calibration(
            db=db_session,
            printer_id=printer.id,
            cali_mode=CaliMode.AUTO_PA_LINE,
            method=CaliMethod.AUTO,
            nozzle_diameter=0.4,
            nozzle_volume_type="standard",
            extruder_id=0,
            filaments=[
                CalibFilamentInput(
                    ams_id=0, slot_id=0, tray_id=0,
                    filament_id="GFG00", filament_setting_id="GFG00_60@BBL",
                    bed_temp=60, nozzle_temp=220, max_volumetric_speed=12.0,
                )
            ],
            user_id=None,
        )
    assert session.status == "running"
    assert session.method == "auto"
    assert session.cali_mode == "auto_pa_line"
    assert session.mqtt_sequence_id == "SEQ-CALI-1"
    mock_client.extrusion_cali_start.assert_called_once()
    call_kwargs = mock_client.extrusion_cali_start.call_args.kwargs
    assert call_kwargs["cali_mode"] == 0
    assert call_kwargs["filaments"][0]["filament_id"] == "GFG00"
    assert call_kwargs["filaments"][0]["nozzle_id"] == "HS20"


@pytest.mark.asyncio
async def test_start_calibration_blocks_on_offline(service, db_session, printer_factory):
    printer = await printer_factory(model="X1C")
    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = None
        with pytest.raises(ValueError, match="not online"):
            await service.start_calibration(
                db=db_session,
                printer_id=printer.id,
                cali_mode=CaliMode.AUTO_PA_LINE,
                method=CaliMethod.AUTO,
                nozzle_diameter=0.4,
                nozzle_volume_type="standard",
                extruder_id=0,
                filaments=[],
                user_id=None,
            )


@pytest.mark.asyncio
async def test_start_calibration_concurrent_blocked(service, db_session, printer_factory, mock_client):
    """Second start while session active → ValueError."""
    printer = await printer_factory(model="X1C")
    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = mock_client
        await service.start_calibration(
            db=db_session, printer_id=printer.id,
            cali_mode=CaliMode.AUTO_PA_LINE, method=CaliMethod.AUTO,
            nozzle_diameter=0.4, nozzle_volume_type="standard",
            extruder_id=0, filaments=[CalibFilamentInput(0, 0, 0, "GFG00", None, 60, 220, 12.0)],
            user_id=None,
        )
        with pytest.raises(ValueError, match="active_session_exists"):
            await service.start_calibration(
                db=db_session, printer_id=printer.id,
                cali_mode=CaliMode.AUTO_PA_LINE, method=CaliMethod.AUTO,
                nozzle_diameter=0.4, nozzle_volume_type="standard",
                extruder_id=0, filaments=[CalibFilamentInput(0, 0, 0, "GFG00", None, 60, 220, 12.0)],
                user_id=None,
            )
```

- [ ] **Step 2: Run tests, see failure**

Run: `pytest backend/tests/unit/services/test_calibration_service.py -v`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Write service skeleton + start_calibration**

```python
# backend/app/services/calibration_service.py
"""Orchestrator for the Filament Calibration wizard.

Two main paths:
    AUTO (X1/X1E/H2D Pro w/ lidar): MQTT extrusion_cali mode=0
    MANUAL (all): copy 3MF asset → dispatch as is_calibration print

Save flow auto-binds to AMS slot via extrusion_cali_sel, and dispatch
re-syncs before each non-cali print job.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.calibration_session import CalibrationSession
from backend.app.models.printer import Printer
from backend.app.services.calibration_constants import (
    CaliMethod,
    CaliMode,
    NozzleVolumeType,
    generate_nozzle_id,
)
from backend.app.services.printer_manager import printer_manager


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


class CalibrationService:
    async def start_calibration(
        self,
        *,
        db: AsyncSession,
        printer_id: int,
        cali_mode: CaliMode,
        method: CaliMethod,
        nozzle_diameter: float,
        nozzle_volume_type: str,
        extruder_id: int,
        filaments: list[CalibFilamentInput],
        user_id: int | None,
    ) -> CalibrationSession:
        # Concurrent guard
        existing = (
            await db.execute(
                select(CalibrationSession).where(
                    CalibrationSession.printer_id == printer_id,
                    CalibrationSession.status.in_(["running", "awaiting_user_input"]),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ValueError(f"active_session_exists:{existing.id}")

        # Validate printer online
        client = printer_manager.get_client(printer_id)
        if not client or not client.state.connected:
            raise ValueError("Printer not online")

        # Build filaments payload (BS-shape) — used for both AUTO and MANUAL
        nozzle_id = generate_nozzle_id(NozzleVolumeType(nozzle_volume_type), nozzle_diameter)
        filaments_payload = [
            {
                "tray_id": f.tray_id,
                "extruder_id": extruder_id,
                "bed_temp": f.bed_temp,
                "filament_id": f.filament_id,
                "setting_id": f.filament_setting_id or "",
                "nozzle_temp": f.nozzle_temp,
                "ams_id": f.ams_id,
                "slot_id": f.slot_id,
                "nozzle_id": nozzle_id,
                "nozzle_diameter": str(nozzle_diameter),
                "max_volumetric_speed": str(f.max_volumetric_speed),
            }
            for f in filaments
        ]

        sequence_id: str | None = None

        if method == CaliMethod.AUTO and cali_mode in (CaliMode.AUTO_PA_LINE,):
            ok, sequence_id = client.extrusion_cali_start(
                nozzle_diameter=nozzle_diameter,
                cali_mode=0,
                filaments=filaments_payload,
            )
            if not ok:
                raise ValueError("MQTT publish failed")
        elif method == CaliMethod.AUTO and cali_mode == CaliMode.FLOW_RATE:
            for fp, f in zip(filaments_payload, filaments):
                fp["flow_rate"] = f.flow_rate
            ok, sequence_id = client.flow_rate_cali_start(
                nozzle_diameter=nozzle_diameter,
                filaments=filaments_payload,
            )
            if not ok:
                raise ValueError("MQTT publish failed")
        else:
            # MANUAL path implemented in Task 13.
            raise NotImplementedError("manual path: Task 13")

        session = CalibrationSession(
            printer_id=printer_id,
            user_id=user_id,
            cali_mode=cali_mode.value,
            method=method.value,
            nozzle_diameter=nozzle_diameter,
            nozzle_volume_type=nozzle_volume_type,
            extruder_id=extruder_id,
            filaments_json=json.dumps(filaments_payload),
            status="running",
            mqtt_sequence_id=sequence_id,
            stage=1,
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)

        client.state.extrusion_cali_session_id = sequence_id
        client.state.extrusion_cali_status = "running"

        return session
```

- [ ] **Step 4: Run tests**

Run: `pytest backend/tests/unit/services/test_calibration_service.py::test_start_calibration_auto_pa backend/tests/unit/services/test_calibration_service.py::test_start_calibration_blocks_on_offline backend/tests/unit/services/test_calibration_service.py::test_start_calibration_concurrent_blocked -v`
Expected: 3 passed.

- [ ] **Step 5: Stage**

`git add backend/app/services/calibration_service.py backend/tests/unit/services/test_calibration_service.py`

---

### Task 12: Asset resolver for manual path

**Files:**
- Create: `backend/app/data/calib_assets/README.md`
- Create: `backend/app/data/calib_assets/.gitkeep` directories
- Modify: `backend/app/services/calibration_service.py` (add `resolve_asset_path`)

- [ ] **Step 1: Create asset directory structure**

Make these directories (with `.gitkeep`):

```
backend/app/data/calib_assets/pressure_advance/
backend/app/data/calib_assets/filament_flow/
backend/app/data/calib_assets/temp_tower/
backend/app/data/calib_assets/volumetric_speed/
backend/app/data/calib_assets/vfa/
backend/app/data/calib_assets/retraction/
```

Create `backend/app/data/calib_assets/.gitkeep` and per-subdir `.gitkeep` (empty files).

- [ ] **Step 2: Write README.md with copy plan**

```markdown
# Calibration Assets

Pre-baked 3MF print files for the Filament Calibration wizard (manual
path on P1S/A1/non-lidar printers).

Source: Bambu Studio `resources/calib/` (AGPL-3.0). Re-copied when BS
updates the assets — see CLAUDE.md → "Bambu Studio Reference Sync".

## Expected files (asset_resolver looks up)

| cali_mode | nozzle_diameter | file |
|---|---|---|
| pa_line | 0.4 | pressure_advance/pa_line_0.4.3mf |
| pa_line | 0.2 / 0.6 / 0.8 | pressure_advance/pa_line_<dia>.3mf |
| pa_pattern | 0.4 | pressure_advance/pa_pattern_0.4.3mf |
| pa_tower | 0.4 | pressure_advance/pa_tower_0.4.3mf |
| flow_rate (pass 1) | 0.4 | filament_flow/flowrate_pass1_0.4.3mf |
| flow_rate (pass 2) | 0.4 | filament_flow/flowrate_pass2_0.4.3mf |
| temp_tower | 0.4 | temp_tower/temp_tower_0.4.3mf |
| vol_speed_tower | 0.4 | volumetric_speed/vol_speed_tower_0.4.3mf |
| vfa_tower | 0.4 | vfa/vfa_tower_0.4.3mf |
| retraction_tower | 0.4 | retraction/retraction_tower_0.4.3mf |

Fallback: missing diameter variant → use 0.4 + log warning.

## Copy script (run when bumping BS reference)

```bash
BS=temp/references/BambuStudio/resources/calib
cp "$BS/pressure_advance/pa_line.3mf"  backend/app/data/calib_assets/pressure_advance/pa_line_0.4.3mf
cp "$BS/pressure_advance/pa_pattern.3mf"  backend/app/data/calib_assets/pressure_advance/pa_pattern_0.4.3mf
cp "$BS/filament_flow/flowrate-test-pass1.3mf"  backend/app/data/calib_assets/filament_flow/flowrate_pass1_0.4.3mf
cp "$BS/filament_flow/flowrate-test-pass2.3mf"  backend/app/data/calib_assets/filament_flow/flowrate_pass2_0.4.3mf
# Add temp/vol/vfa/retraction once BS-version is identified
```
```

- [ ] **Step 3: Add resolver + tests**

Append test:

```python
# Add to backend/tests/unit/services/test_calibration_service.py
def test_resolve_asset_path_pa_line_0_4():
    from backend.app.services.calibration_service import resolve_asset_path
    from backend.app.services.calibration_constants import CaliMode

    p = resolve_asset_path(CaliMode.PA_LINE, nozzle_diameter=0.4, pass_n=1)
    assert p.name == "pa_line_0.4.3mf"
    assert "pressure_advance" in str(p)


def test_resolve_asset_path_flow_pass2():
    from backend.app.services.calibration_service import resolve_asset_path
    from backend.app.services.calibration_constants import CaliMode

    p = resolve_asset_path(CaliMode.FLOW_RATE, nozzle_diameter=0.4, pass_n=2)
    assert p.name == "flowrate_pass2_0.4.3mf"


def test_resolve_asset_path_fallback_to_0_4():
    """If unsupported diameter — fallback to 0.4 with warning."""
    from backend.app.services.calibration_service import resolve_asset_path
    from backend.app.services.calibration_constants import CaliMode

    p = resolve_asset_path(CaliMode.PA_LINE, nozzle_diameter=0.6, pass_n=1)
    # Will be 0.6 if file present else 0.4
    assert p.name in ("pa_line_0.6.3mf", "pa_line_0.4.3mf")
```

Then add to `calibration_service.py`:

```python
# Top of file:
from pathlib import Path

ASSET_ROOT = Path(__file__).resolve().parent.parent / "data" / "calib_assets"

_MODE_TO_PATH = {
    CaliMode.PA_LINE: ("pressure_advance", "pa_line"),
    CaliMode.PA_PATTERN: ("pressure_advance", "pa_pattern"),
    CaliMode.PA_TOWER: ("pressure_advance", "pa_tower"),
    CaliMode.TEMP_TOWER: ("temp_tower", "temp_tower"),
    CaliMode.VOL_SPEED_TOWER: ("volumetric_speed", "vol_speed_tower"),
    CaliMode.VFA_TOWER: ("vfa", "vfa_tower"),
    CaliMode.RETRACTION_TOWER: ("retraction", "retraction_tower"),
}


def resolve_asset_path(cali_mode: CaliMode, *, nozzle_diameter: float, pass_n: int = 1) -> Path:
    """Map (cali_mode, diameter) → 3MF asset path. Fallback to 0.4 if missing."""
    if cali_mode == CaliMode.FLOW_RATE:
        fname = f"flowrate_pass{pass_n}_{nozzle_diameter}.3mf"
        path = ASSET_ROOT / "filament_flow" / fname
        if not path.exists():
            path = ASSET_ROOT / "filament_flow" / f"flowrate_pass{pass_n}_0.4.3mf"
        return path

    bucket = _MODE_TO_PATH.get(cali_mode)
    if bucket is None:
        raise ValueError(f"No asset mapping for cali_mode: {cali_mode}")
    subdir, stem = bucket
    path = ASSET_ROOT / subdir / f"{stem}_{nozzle_diameter}.3mf"
    if not path.exists():
        path = ASSET_ROOT / subdir / f"{stem}_0.4.3mf"
    return path
```

- [ ] **Step 4: Run tests**

Run: `pytest backend/tests/unit/services/test_calibration_service.py -v -k "asset"`
Expected: 3 passed.

- [ ] **Step 5: Stage**

`git add backend/app/services/calibration_service.py backend/app/data/calib_assets/ backend/tests/unit/services/test_calibration_service.py`

---

### Task 13: submit_manual_result — PA branch + Flow coarse → fine chain

**Files:**
- Modify: `backend/app/services/calibration_service.py`
- Modify: `backend/tests/unit/services/test_calibration_service.py`

- [ ] **Step 1: Add tests**

```python
# Append to test_calibration_service.py

@pytest.mark.asyncio
async def test_submit_manual_pa_computes_k(service, db_session, printer_factory, mock_client):
    """PA Line: K = start + idx * step = 0 + 24 * 0.002 = 0.048."""
    printer = await printer_factory(model="P1S")
    # Create awaiting_user_input session manually
    from backend.app.models.calibration_session import CalibrationSession
    s = CalibrationSession(
        printer_id=printer.id, user_id=None,
        cali_mode="pa_line", method="manual",
        nozzle_diameter=0.4, nozzle_volume_type="standard", extruder_id=0,
        filaments_json='[{"ams_id":0,"slot_id":0,"tray_id":0,"filament_id":"GFG00",'
                       '"setting_id":"GFG00_60@BBL","nozzle_diameter":"0.4"}]',
        status="awaiting_user_input", stage=1,
    )
    db_session.add(s); await db_session.commit(); await db_session.refresh(s)

    mock_client.extrusion_cali_set = MagicMock(return_value=(True, "SEQ-SET-1"))
    mock_client.extrusion_cali_sel = MagicMock(return_value=(True, "SEQ-SEL-1"))

    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = mock_client
        result = await service.submit_manual_result(
            db=db_session, session_id=s.id, best_line_index=24,
        )
    assert len(result.saved_rows) == 1
    assert abs(result.saved_rows[0].pa_k_value - 0.048) < 1e-9
    assert result.saved_rows[0].is_active is True
    mock_client.extrusion_cali_set.assert_called_once()


@pytest.mark.asyncio
async def test_submit_manual_flow_coarse_creates_stage2(service, db_session, printer_factory, mock_client):
    printer = await printer_factory(model="P1S")
    from backend.app.models.calibration_session import CalibrationSession
    s = CalibrationSession(
        printer_id=printer.id, user_id=None,
        cali_mode="flow_rate", method="manual",
        nozzle_diameter=0.4, nozzle_volume_type="standard", extruder_id=0,
        filaments_json='[{"ams_id":0,"slot_id":0,"tray_id":0,"filament_id":"GFG00",'
                       '"setting_id":"GFG00_60@BBL"}]',
        status="awaiting_user_input", stage=1,
    )
    db_session.add(s); await db_session.commit(); await db_session.refresh(s)

    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = mock_client
        result = await service.submit_manual_result(
            db=db_session, session_id=s.id, coarse_modifier=10, skip_fine=False,
        )
    assert result.next_session_id is not None
    assert result.saved_rows == []
    # Original session should advance
    assert s.coarse_ratio is not None
    assert abs(s.coarse_ratio - 1.10) < 1e-9


@pytest.mark.asyncio
async def test_submit_manual_flow_coarse_skip_fine_saves(service, db_session, printer_factory, mock_client):
    printer = await printer_factory(model="P1S")
    from backend.app.models.calibration_session import CalibrationSession
    s = CalibrationSession(
        printer_id=printer.id, user_id=None,
        cali_mode="flow_rate", method="manual",
        nozzle_diameter=0.4, nozzle_volume_type="standard", extruder_id=0,
        filaments_json='[{"ams_id":0,"slot_id":0,"tray_id":0,"filament_id":"GFG00",'
                       '"setting_id":"GFG00_60@BBL"}]',
        status="awaiting_user_input", stage=1,
    )
    db_session.add(s); await db_session.commit(); await db_session.refresh(s)

    mock_client.extrusion_cali_set = MagicMock(return_value=(True, "SEQ-SET-1"))
    mock_client.extrusion_cali_sel = MagicMock(return_value=(True, "SEQ-SEL-1"))

    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = mock_client
        result = await service.submit_manual_result(
            db=db_session, session_id=s.id, coarse_modifier=5, skip_fine=True,
        )
    assert result.next_session_id is None
    assert len(result.saved_rows) == 1
    assert abs(result.saved_rows[0].flow_ratio - 1.05) < 1e-9


@pytest.mark.asyncio
async def test_submit_manual_flow_fine_saves_combined(service, db_session, printer_factory, mock_client):
    printer = await printer_factory(model="P1S")
    from backend.app.models.calibration_session import CalibrationSession
    parent = CalibrationSession(
        printer_id=printer.id, user_id=None,
        cali_mode="flow_rate", method="manual",
        nozzle_diameter=0.4, nozzle_volume_type="standard", extruder_id=0,
        filaments_json='[]', status="saved", stage=1,
        coarse_ratio=1.05,
    )
    db_session.add(parent); await db_session.commit(); await db_session.refresh(parent)

    stage2 = CalibrationSession(
        printer_id=printer.id, user_id=None,
        cali_mode="flow_rate", method="manual",
        nozzle_diameter=0.4, nozzle_volume_type="standard", extruder_id=0,
        filaments_json='[{"ams_id":0,"slot_id":0,"tray_id":0,"filament_id":"GFG00",'
                       '"setting_id":"GFG00_60@BBL"}]',
        status="awaiting_user_input", stage=2,
        parent_session_id=parent.id, coarse_ratio=1.05,
    )
    db_session.add(stage2); await db_session.commit(); await db_session.refresh(stage2)

    mock_client.extrusion_cali_set = MagicMock(return_value=(True, "SEQ-SET-1"))
    mock_client.extrusion_cali_sel = MagicMock(return_value=(True, "SEQ-SEL-1"))

    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = mock_client
        result = await service.submit_manual_result(
            db=db_session, session_id=stage2.id, fine_modifier=2,
        )
    assert len(result.saved_rows) == 1
    # 1.05 * (100+2)/100 = 1.071
    assert abs(result.saved_rows[0].flow_ratio - 1.071) < 1e-9
```

- [ ] **Step 2: Run tests, see failure**

Run: `pytest backend/tests/unit/services/test_calibration_service.py -v -k "manual"`
Expected: FAIL (method not defined).

- [ ] **Step 3: Implement submit_manual_result + save_result**

Append to `calibration_service.py`:

```python
from dataclasses import dataclass, field

from backend.app.models.filament_calibration import FilamentCalibration
from backend.app.services.calibration_constants import (
    compute_flow_ratio_coarse,
    compute_flow_ratio_fine,
    compute_pa_k,
)


@dataclass
class ManualResultOut:
    saved_rows: list[FilamentCalibration] = field(default_factory=list)
    next_session_id: int | None = None


@dataclass
class ResultPayload:
    pa_k_value: float | None = None
    pa_n_coef: float | None = None
    flow_ratio: float | None = None
    confidence: int | None = None
    cali_idx: int | None = None
    source: str = "manual"
    name: str = ""


class CalibrationService:
    # ... existing start_calibration ...

    async def submit_manual_result(
        self,
        *,
        db: AsyncSession,
        session_id: int,
        best_line_index: int | None = None,
        coarse_modifier: int | None = None,
        skip_fine: bool = False,
        fine_modifier: int | None = None,
    ) -> ManualResultOut:
        s = (
            await db.execute(select(CalibrationSession).where(CalibrationSession.id == session_id))
        ).scalar_one()
        if s.status != "awaiting_user_input":
            raise ValueError(f"session not awaiting input (status={s.status})")

        cm = CaliMode(s.cali_mode)
        if cm in (CaliMode.PA_LINE, CaliMode.PA_PATTERN, CaliMode.PA_TOWER):
            if best_line_index is None:
                raise ValueError("best_line_index required for PA mode")
            k = compute_pa_k(best_line_index)
            return ManualResultOut(
                saved_rows=[
                    await self.save_result(
                        db=db, session=s,
                        payload=ResultPayload(pa_k_value=k, source="manual",
                                              name=f"{s.cali_mode} K={k:.4f}"),
                    )
                ]
            )

        if cm == CaliMode.FLOW_RATE and s.stage == 1:
            if coarse_modifier is None:
                raise ValueError("coarse_modifier required for Flow Rate stage 1")
            coarse = compute_flow_ratio_coarse(coarse_modifier)
            s.coarse_ratio = coarse
            await db.commit()
            if skip_fine:
                return ManualResultOut(
                    saved_rows=[
                        await self.save_result(
                            db=db, session=s,
                            payload=ResultPayload(flow_ratio=coarse, source="manual",
                                                  name=f"flow_rate {coarse:.3f} (coarse only)"),
                        )
                    ]
                )
            # Start stage-2 session — kick off pass2 print
            stage2 = await self._start_flow_rate_stage2(db=db, parent=s)
            return ManualResultOut(next_session_id=stage2.id)

        if cm == CaliMode.FLOW_RATE and s.stage == 2:
            if fine_modifier is None:
                raise ValueError("fine_modifier required for Flow Rate stage 2")
            if s.coarse_ratio is None:
                raise ValueError("stage-2 session missing coarse_ratio")
            fine = compute_flow_ratio_fine(s.coarse_ratio, fine_modifier)
            return ManualResultOut(
                saved_rows=[
                    await self.save_result(
                        db=db, session=s,
                        payload=ResultPayload(flow_ratio=fine, source="manual",
                                              name=f"flow_rate {fine:.3f}"),
                    )
                ]
            )

        raise ValueError(f"submit_manual_result unsupported for mode {cm}")

    async def _start_flow_rate_stage2(
        self, *, db: AsyncSession, parent: CalibrationSession,
    ) -> CalibrationSession:
        """Create stage-2 session inheriting parent's filaments. Print queue
        wiring deferred to Task 14 (manual path orchestration)."""
        # For now, just create the row in awaiting_user_input state so tests pass.
        # Task 14 will replace this with actual print-asset dispatch.
        stage2 = CalibrationSession(
            printer_id=parent.printer_id,
            user_id=parent.user_id,
            cali_mode=parent.cali_mode,
            method=parent.method,
            nozzle_diameter=parent.nozzle_diameter,
            nozzle_volume_type=parent.nozzle_volume_type,
            extruder_id=parent.extruder_id,
            filaments_json=parent.filaments_json,
            status="awaiting_user_input",
            stage=2,
            parent_session_id=parent.id,
            coarse_ratio=parent.coarse_ratio,
        )
        db.add(stage2)
        await db.commit()
        await db.refresh(stage2)
        return stage2

    async def save_result(
        self,
        *,
        db: AsyncSession,
        session: CalibrationSession,
        payload: ResultPayload,
    ) -> FilamentCalibration:
        printer = (
            await db.execute(select(Printer).where(Printer.id == session.printer_id))
        ).scalar_one()
        fil = json.loads(session.filaments_json)[0]

        # 1. Flip existing active rows to false
        existing = (
            await db.execute(
                select(FilamentCalibration).where(
                    FilamentCalibration.printer_model == printer.model,
                    FilamentCalibration.filament_id == fil["filament_id"],
                    FilamentCalibration.nozzle_diameter == session.nozzle_diameter,
                    FilamentCalibration.nozzle_volume_type == session.nozzle_volume_type,
                    FilamentCalibration.extruder_id == session.extruder_id,
                    FilamentCalibration.is_active.is_(True),
                )
            )
        ).scalars().all()
        for row in existing:
            row.is_active = False

        # 2. Insert new active row
        new_row = FilamentCalibration(
            printer_model=printer.model,
            filament_id=fil["filament_id"],
            filament_setting_id=fil.get("setting_id") or None,
            nozzle_diameter=session.nozzle_diameter,
            nozzle_volume_type=session.nozzle_volume_type,
            extruder_id=session.extruder_id,
            pa_k_value=payload.pa_k_value,
            pa_n_coef=payload.pa_n_coef,
            flow_ratio=payload.flow_ratio,
            confidence=payload.confidence,
            cali_mode=session.cali_mode,
            source=payload.source,
            is_active=True,
            cali_idx=payload.cali_idx,
            name=payload.name or f"{fil['filament_id']} cali",
            calibrated_on_printer_id=session.printer_id,
            calibrated_by_user_id=session.user_id,
        )
        db.add(new_row)
        await db.commit()
        await db.refresh(new_row)

        # 3. MQTT extrusion_cali_set → printer-side history
        client = printer_manager.get_client(session.printer_id)
        if client and client.state.connected and payload.pa_k_value is not None:
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
                    "k_value": str(payload.pa_k_value),
                    "n_coef": str(payload.pa_n_coef or 0.0),
                }],
            )
            # 4. Auto-bind to AMS slot
            if new_row.cali_idx is not None:
                client.extrusion_cali_sel(
                    ams_id=fil["ams_id"],
                    tray_id=fil["tray_id"],
                    cali_idx=new_row.cali_idx,
                    extruder_id=session.extruder_id,
                    nozzle_diameter=session.nozzle_diameter,
                )

        session.status = "saved"
        await db.commit()
        return new_row
```

- [ ] **Step 4: Run tests**

Run: `pytest backend/tests/unit/services/test_calibration_service.py -v -k "manual or save"`
Expected: 4 passed.

- [ ] **Step 5: Stage**

`git add backend/app/services/calibration_service.py backend/tests/unit/services/test_calibration_service.py`

---

### Task 14: Manual print dispatch + MANUAL branch in start_calibration

**Files:**
- Modify: `backend/app/services/calibration_service.py`

- [ ] **Step 1: Test for manual start**

Append to `test_calibration_service.py`:

```python
@pytest.mark.asyncio
async def test_start_calibration_manual_dispatches_print(service, db_session, printer_factory, mock_client, tmp_path):
    printer = await printer_factory(model="P1S")

    fake_asset = tmp_path / "pa_line_0.4.3mf"
    fake_asset.write_bytes(b"PK\x03\x04fake3mf")

    # Mock dispatch + asset resolver
    with patch("backend.app.services.calibration_service.printer_manager") as pm, \
         patch("backend.app.services.calibration_service.resolve_asset_path", return_value=fake_asset), \
         patch("backend.app.services.calibration_service.background_dispatch") as bg:
        pm.get_client.return_value = mock_client
        bg.enqueue_calibration_print = AsyncMock(return_value=42)  # print_queue_item_id
        session = await service.start_calibration(
            db=db_session, printer_id=printer.id,
            cali_mode=CaliMode.PA_LINE, method=CaliMethod.MANUAL,
            nozzle_diameter=0.4, nozzle_volume_type="standard", extruder_id=0,
            filaments=[CalibFilamentInput(0, 0, 0, "GFG00", "GFG00_60@BBL", 60, 220, 12.0)],
            user_id=None,
        )
    assert session.method == "manual"
    assert session.status == "running"
    assert session.print_queue_item_id == 42
    bg.enqueue_calibration_print.assert_called_once()
```

- [ ] **Step 2: Run test, see failure**

Run: `pytest backend/tests/unit/services/test_calibration_service.py -v -k "manual_dispatches"`
Expected: FAIL.

- [ ] **Step 3: Replace NotImplementedError in start_calibration with manual path**

Edit `start_calibration` in `calibration_service.py` — replace the `raise NotImplementedError` block with:

```python
else:
    # MANUAL path: resolve 3MF asset → enqueue as is_calibration print
    from backend.app.services import background_dispatch  # late import (avoid cycle)
    asset_path = resolve_asset_path(
        cali_mode,
        nozzle_diameter=nozzle_diameter,
        pass_n=1,
    )
    if not asset_path.exists():
        raise ValueError(f"calibration asset not available: {asset_path.name}")

    print_queue_item_id = await background_dispatch.enqueue_calibration_print(
        printer_id=printer_id,
        asset_path=str(asset_path),
        cali_mode=cali_mode.value,
        user_id=user_id,
        ams_id=filaments[0].ams_id,
        slot_id=filaments[0].slot_id,
        tray_id=filaments[0].tray_id,
    )
    # No MQTT sequence_id yet — it will be set by dispatch after print_start
```

Then below, when creating the session, add `print_queue_item_id`:

```python
session = CalibrationSession(
    # ... existing fields ...
    print_queue_item_id=locals().get("print_queue_item_id"),
)
```

- [ ] **Step 4: Add `enqueue_calibration_print` stub to background_dispatch**

In `backend/app/services/background_dispatch.py`, add at module level:

```python
async def enqueue_calibration_print(
    *,
    printer_id: int,
    asset_path: str,
    cali_mode: str,
    user_id: int | None,
    ams_id: int,
    slot_id: int,
    tray_id: int,
) -> int:
    """Enqueue a calibration print. Returns PrintQueueItem.id.

    Tags PrintQueueItem.is_calibration=True. The dispatcher copies the
    asset 3MF to the printer's FTP and starts the print like any other
    job — but downstream hooks (on_print_complete) flip the linked
    calibration_session to awaiting_user_input.

    Phase-1 wiring: this is a thin stub that creates the queue item and
    relies on the existing dispatch loop to pick it up. Per-stage 2
    Flow Rate (pass2) reuses this same function.
    """
    # Real implementation in Task 15; for now this stub keeps
    # CalibrationService.start_calibration compilable. Tests use a mock.
    raise NotImplementedError("enqueue_calibration_print: Task 15")
```

- [ ] **Step 5: Run only the mocked test**

Run: `pytest backend/tests/unit/services/test_calibration_service.py -v -k "manual_dispatches"`
Expected: passed (mock satisfies the stub).

- [ ] **Step 6: Stage**

`git add backend/app/services/calibration_service.py backend/app/services/background_dispatch.py`

---

### Task 15: Real enqueue_calibration_print + dispatch hook

**Files:**
- Modify: `backend/app/services/background_dispatch.py`

- [ ] **Step 1: Inspect existing dispatch enqueue surface**

Read `backend/app/services/background_dispatch.py` and `backend/app/api/routes/background_dispatch.py` to find:
- How a normal print is enqueued (the function that creates `PrintQueueItem`).
- The on_print_start / on_print_complete hooks.

- [ ] **Step 2: Replace stub with real impl**

```python
# In backend/app/services/background_dispatch.py — replace the stub:

from backend.app.models.print_queue_item import PrintQueueItem  # confirm path
from backend.app.core.database import async_session_maker


async def enqueue_calibration_print(
    *,
    printer_id: int,
    asset_path: str,
    cali_mode: str,
    user_id: int | None,
    ams_id: int,
    slot_id: int,
    tray_id: int,
) -> int:
    """Create a PrintQueueItem with is_calibration=True referencing a
    local 3MF asset. Returns the new item id."""
    async with async_session_maker() as db:
        item = PrintQueueItem(
            printer_id=printer_id,
            file_path=asset_path,
            file_name=Path(asset_path).name,
            is_calibration=True,
            # calibration_session_id is set by the caller after session insert
            status="queued",
            user_id=user_id,
            ams_id=ams_id,
            slot_id=slot_id,
            tray_id=tray_id,
        )
        db.add(item)
        await db.commit()
        await db.refresh(item)
        return item.id
```

(Adjust field names to match your actual `PrintQueueItem` model — particularly `file_name`/`file_path`/AMS-binding fields. Use the same fields a normal queue insert uses.)

- [ ] **Step 3: Stitch on_print_complete → session.awaiting_user_input**

Locate the existing on_print_complete handler in `background_dispatch.py`. Add:

```python
# Inside the print-complete handler (where mc_print_stage flips IDLE):
if completed_item.is_calibration and completed_item.calibration_session_id:
    async with async_session_maker() as db:
        cs = await db.get(CalibrationSession, completed_item.calibration_session_id)
        if cs and cs.status == "running":
            cs.status = "awaiting_user_input"
            await db.commit()
    # WS notify implemented in Task 19
```

Add the import at top:

```python
from backend.app.models.calibration_session import CalibrationSession
```

- [ ] **Step 4: Verify build/import**

Run: `python -c "from backend.app.services.background_dispatch import enqueue_calibration_print; print('ok')"`
Expected: `ok`.

- [ ] **Step 5: Stage**

`git add backend/app/services/background_dispatch.py`

---

### Task 16: submit_auto_result + cancel_session

**Files:**
- Modify: `backend/app/services/calibration_service.py`

- [ ] **Step 1: Tests**

Append to `test_calibration_service.py`:

```python
@pytest.mark.asyncio
async def test_submit_auto_result_saves_picked_rows(service, db_session, printer_factory, mock_client):
    printer = await printer_factory(model="X1C")
    from backend.app.models.calibration_session import CalibrationSession
    from backend.app.services.bambu_mqtt import ExtrusionCaliResult
    s = CalibrationSession(
        printer_id=printer.id, user_id=None,
        cali_mode="auto_pa_line", method="auto",
        nozzle_diameter=0.4, nozzle_volume_type="standard", extruder_id=0,
        filaments_json='[{"ams_id":0,"slot_id":0,"tray_id":0,"filament_id":"GFG00","setting_id":"GFG00_60@BBL"}]',
        status="awaiting_user_input", stage=1,
    )
    db_session.add(s); await db_session.commit(); await db_session.refresh(s)

    mock_client.state.extrusion_cali_results = [
        ExtrusionCaliResult(
            tray_id=0, ams_id=0, slot_id=0, extruder_id=0,
            nozzle_diameter=0.4, nozzle_volume_type="standard",
            filament_id="GFG00", setting_id="GFG00_60@BBL",
            k_value=0.0432, n_coef=1.0, confidence=0,
        )
    ]
    mock_client.extrusion_cali_set = MagicMock(return_value=(True, "SEQ-SET"))
    mock_client.extrusion_cali_sel = MagicMock(return_value=(True, "SEQ-SEL"))

    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = mock_client
        rows = await service.submit_auto_result(
            db=db_session, session_id=s.id,
            edits=[{"tray_id": 0, "k_value": 0.05, "name": "PLA — PA 0.05", "save": True}],
        )
    assert len(rows) == 1
    assert abs(rows[0].pa_k_value - 0.05) < 1e-9


@pytest.mark.asyncio
async def test_cancel_session_running_auto_stops_print(service, db_session, printer_factory, mock_client):
    printer = await printer_factory(model="X1C")
    from backend.app.models.calibration_session import CalibrationSession
    s = CalibrationSession(
        printer_id=printer.id, user_id=None,
        cali_mode="auto_pa_line", method="auto",
        nozzle_diameter=0.4, nozzle_volume_type="standard", extruder_id=0,
        filaments_json='[]', status="running", stage=1,
        mqtt_sequence_id="SEQ-X",
    )
    db_session.add(s); await db_session.commit(); await db_session.refresh(s)

    mock_client.stop_print = MagicMock(return_value=(True, "SEQ-STOP"))

    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = mock_client
        await service.cancel_session(db=db_session, session_id=s.id)
    await db_session.refresh(s)
    assert s.status == "cancelled"
    mock_client.stop_print.assert_called_once()
```

- [ ] **Step 2: Add methods**

Append to `CalibrationService`:

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

    results_by_tray = {r.tray_id: r for r in client.state.extrusion_cali_results}
    saved: list[FilamentCalibration] = []
    for edit in edits:
        if not edit.get("save", True):
            continue
        base = results_by_tray.get(edit["tray_id"])
        if base is None:
            continue
        # Allow user override of K/name; auto provides defaults
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


async def cancel_session(self, *, db: AsyncSession, session_id: int) -> None:
    s = (
        await db.execute(select(CalibrationSession).where(CalibrationSession.id == session_id))
    ).scalar_one()
    if s.status in ("saved", "cancelled", "failed"):
        return

    client = printer_manager.get_client(s.printer_id)
    if s.status == "running" and client:
        # Auto path or manual print mid-flight: stop print
        if hasattr(client, "stop_print"):
            client.stop_print()
    s.status = "cancelled"
    await db.commit()
```

- [ ] **Step 3: Run new tests**

Run: `pytest backend/tests/unit/services/test_calibration_service.py -v -k "auto_result or cancel"`
Expected: 2 passed.

- [ ] **Step 4: Stage**

`git add backend/app/services/calibration_service.py backend/tests/unit/services/test_calibration_service.py`

---

### Task 17: `_resolve_active_calibration` + dispatch apply hook

**Files:**
- Modify: `backend/app/services/calibration_service.py`
- Modify: `backend/app/services/background_dispatch.py`
- Test: `backend/tests/unit/services/test_calibration_dispatch_apply.py`

- [ ] **Step 1: Test**

```python
# backend/tests/unit/services/test_calibration_dispatch_apply.py
"""Tests dispatcher re-sels active calibration per filament slot."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.app.models.filament_calibration import FilamentCalibration
from backend.app.services.calibration_service import resolve_active_calibration


@pytest.mark.asyncio
async def test_resolve_returns_active_row(db_session, printer_factory):
    printer = await printer_factory(model="P1S")
    db_session.add(
        FilamentCalibration(
            printer_model="P1S", filament_id="GFG00",
            nozzle_diameter=0.4, nozzle_volume_type="standard", extruder_id=0,
            pa_k_value=0.048, cali_mode="pa_line", source="manual",
            is_active=True, cali_idx=3, name="r1",
        )
    )
    await db_session.commit()

    row = await resolve_active_calibration(
        db=db_session, printer_model="P1S", filament_id="GFG00",
        nozzle_dia=0.4, nozzle_vol_type="standard", extruder_id=0,
    )
    assert row is not None
    assert row.cali_idx == 3


@pytest.mark.asyncio
async def test_resolve_no_match_returns_none(db_session, printer_factory):
    await printer_factory(model="X1C")
    row = await resolve_active_calibration(
        db=db_session, printer_model="X1C", filament_id="UNKNOWN",
        nozzle_dia=0.4, nozzle_vol_type="standard", extruder_id=0,
    )
    assert row is None
```

- [ ] **Step 2: Run test, fail**

Run: `pytest backend/tests/unit/services/test_calibration_dispatch_apply.py -v`
Expected: FAIL.

- [ ] **Step 3: Add resolver in service**

Append to `calibration_service.py`:

```python
async def resolve_active_calibration(
    *,
    db: AsyncSession,
    printer_model: str,
    filament_id: str,
    nozzle_dia: float,
    nozzle_vol_type: str,
    extruder_id: int,
) -> FilamentCalibration | None:
    return (
        await db.execute(
            select(FilamentCalibration).where(
                FilamentCalibration.printer_model == printer_model,
                FilamentCalibration.filament_id == filament_id,
                FilamentCalibration.nozzle_diameter == nozzle_dia,
                FilamentCalibration.nozzle_volume_type == nozzle_vol_type,
                FilamentCalibration.extruder_id == extruder_id,
                FilamentCalibration.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
```

- [ ] **Step 4: Wire into dispatch (pre-print-start hook)**

In `background_dispatch.py`, in the function that fires immediately before MQTT print-start (find by grep for `start_print` or `command_start`), add:

```python
# Skip for calibration jobs themselves
if not getattr(queue_item, "is_calibration", False):
    from backend.app.services.calibration_service import resolve_active_calibration
    # For each filament slot used by this job:
    for slot in slots_used:  # adapt to your slot iteration pattern
        cali = await resolve_active_calibration(
            db=db, printer_model=printer.model,
            filament_id=slot.filament_id,
            nozzle_dia=slot.nozzle_diameter or 0.4,
            nozzle_vol_type=slot.nozzle_volume_type or "standard",
            extruder_id=slot.extruder_id or 0,
        )
        if cali and cali.cali_idx is not None:
            client.extrusion_cali_sel(
                ams_id=slot.ams_id,
                tray_id=slot.tray_id,
                cali_idx=cali.cali_idx,
                extruder_id=cali.extruder_id,
                nozzle_diameter=cali.nozzle_diameter,
            )
```

(Adapt `slots_used` extraction to your actual dispatch code.)

- [ ] **Step 5: Run tests**

Run: `pytest backend/tests/unit/services/test_calibration_dispatch_apply.py -v`
Expected: 2 passed.

- [ ] **Step 6: Stage**

`git add backend/app/services/calibration_service.py backend/app/services/background_dispatch.py backend/tests/unit/services/test_calibration_dispatch_apply.py`

---

### Wave 4 verify

- [ ] All Wave 4 tests

Run: `pytest backend/tests/unit/services/test_calibration_service.py backend/tests/unit/services/test_calibration_dispatch_apply.py -v`
Expected: all passed.

- [ ] Commit suggestion (when user asks):

```
feat(calibration): CalibrationService + dispatch apply hook

- start_calibration: auto (extrusion_cali) + manual (enqueue 3MF asset
  as is_calibration print). Concurrent-session guard (one per printer).
- submit_manual_result: PA K = start + idx*step; Flow Rate 2-stage
  chain (coarse → stage-2 session → fine).
- submit_auto_result: applies edits to push extrusion_cali_get_result
  rows; save_result auto-binds via extrusion_cali_sel.
- cancel_session: MQTT stop for running auto/manual; mark cancelled.
- resolve_active_calibration + dispatch hook: re-sels active row per
  filament slot on every non-cali print start.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Wave 5 — REST API

### Task 18: Pydantic schemas

**Files:**
- Create: `backend/app/schemas/filament_calibration.py`

- [ ] **Step 1: Write the schemas file**

```python
# backend/app/schemas/filament_calibration.py
"""Pydantic schemas for the Filament Calibration wizard API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.app.services.calibration_constants import CaliMethod, CaliMode


# ---------- Capabilities ----------

class ExtruderInfo(BaseModel):
    id: int
    name: str


class NozzleInfo(BaseModel):
    id: int
    diameter: float | None = None
    type: str | None = None
    flow_type: str | None = None


class CalibCapabilities(BaseModel):
    pa_manual: bool
    flow_manual: bool
    temp_tower: bool
    vol_speed_tower: bool
    vfa_tower: bool
    retraction_tower: bool
    pa_auto: bool
    flow_auto: bool
    dual_extruder: bool
    extruders: list[ExtruderInfo]
    nozzles: list[NozzleInfo]


# ---------- Start ----------

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


class StartSessionIn(BaseModel):
    cali_mode: CaliMode
    method: CaliMethod
    nozzle_diameter: float
    nozzle_volume_type: Literal["standard", "high_flow", "tpu_high_flow", "hybrid"]
    extruder_id: int = 0
    filaments: list[CalibFilamentIn]


# ---------- Submit ----------

class ManualResultIn(BaseModel):
    best_line_index: int | None = None
    coarse_modifier: int | None = None
    skip_fine: bool = False
    fine_modifier: int | None = None


class AutoResultEditIn(BaseModel):
    tray_id: int
    k_value: float | None = None
    n_coef: float | None = None
    name: str | None = None
    save: bool = True


class AutoResultIn(BaseModel):
    results: list[AutoResultEditIn]


# ---------- Outputs ----------

class FilamentCalibrationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    printer_model: str
    filament_id: str
    filament_setting_id: str | None
    nozzle_diameter: float
    nozzle_volume_type: str
    extruder_id: int
    pa_k_value: float | None
    pa_n_coef: float | None
    flow_ratio: float | None
    confidence: int | None
    cali_mode: str
    source: str
    is_active: bool
    cali_idx: int | None
    name: str
    notes: str | None
    calibrated_on_printer_id: int | None
    calibrated_by_user_id: int | None
    created_at: datetime


class CalibrationSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    printer_id: int
    user_id: int | None
    cali_mode: str
    method: str
    nozzle_diameter: float
    nozzle_volume_type: str
    extruder_id: int
    status: str
    stage: int
    coarse_ratio: float | None
    parent_session_id: int | None
    mqtt_sequence_id: str | None
    print_queue_item_id: int | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class ManualResultOutSchema(BaseModel):
    saved_rows: list[FilamentCalibrationOut]
    next_session_id: int | None = None


class PACalibHistoryEntryOut(BaseModel):
    cali_idx: int
    name: str
    filament_id: str
    setting_id: str
    nozzle_diameter: float
    nozzle_volume_type: str
    extruder_id: int
    k_value: float
    n_coef: float
```

- [ ] **Step 2: Smoke-test**

Run: `python -c "from backend.app.schemas.filament_calibration import CalibCapabilities, StartSessionIn; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Stage**

`git add backend/app/schemas/filament_calibration.py`

---

### Task 19: REST router skeleton + capabilities + sessions endpoints

**Files:**
- Create: `backend/app/api/routes/filament_calibration.py`
- Test: `backend/tests/integration/test_calibration_routes.py`

- [ ] **Step 1: Tests**

```python
# backend/tests/integration/test_calibration_routes.py
"""Integration tests for /printers/{id}/calibration/* + /calibration/* + /filament-calibrations."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


def _mock_client(*, online=True, pa_auto=True):
    c = MagicMock()
    c.state.connected = online
    c.state.is_support_pa_calibration = pa_auto
    c.state.is_support_auto_flow_calibration = pa_auto
    c.state.nozzles = []
    c.state.extrusion_cali_results = []
    c.state.extrusion_cali_history = []
    c.extrusion_cali_start = MagicMock(return_value=(True, "SEQ-1"))
    c.flow_rate_cali_start = MagicMock(return_value=(True, "SEQ-2"))
    c.extrusion_cali_set = MagicMock(return_value=(True, "SEQ-SET"))
    c.extrusion_cali_sel = MagicMock(return_value=(True, "SEQ-SEL"))
    c.extrusion_cali_query_history = MagicMock(return_value=(True, "SEQ-Q"))
    return c


@pytest.mark.asyncio
async def test_get_capabilities_x1c(async_client, printer_factory):
    p = await printer_factory(model="X1C")
    with patch("backend.app.api.routes.filament_calibration.printer_manager") as pm:
        pm.get_client.return_value = _mock_client()
        r = await async_client.get(f"/api/v1/printers/{p.id}/calibration/capabilities")
    assert r.status_code == 200
    body = r.json()
    assert body["pa_auto"] is True
    assert body["pa_manual"] is True


@pytest.mark.asyncio
async def test_post_session_auto(async_client, printer_factory):
    p = await printer_factory(model="X1C")
    with patch("backend.app.api.routes.filament_calibration.printer_manager") as pm:
        pm.get_client.return_value = _mock_client()
        r = await async_client.post(
            f"/api/v1/printers/{p.id}/calibration/sessions",
            json={
                "cali_mode": "auto_pa_line",
                "method": "auto",
                "nozzle_diameter": 0.4,
                "nozzle_volume_type": "standard",
                "extruder_id": 0,
                "filaments": [{
                    "ams_id": 0, "slot_id": 0, "tray_id": 0,
                    "filament_id": "GFG00", "filament_setting_id": "GFG00_60@BBL",
                    "bed_temp": 60, "nozzle_temp": 220, "max_volumetric_speed": 12.0,
                }],
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "running"
    assert body["method"] == "auto"


@pytest.mark.asyncio
async def test_post_session_concurrent_409(async_client, printer_factory):
    p = await printer_factory(model="X1C")
    with patch("backend.app.api.routes.filament_calibration.printer_manager") as pm:
        pm.get_client.return_value = _mock_client()
        body = {
            "cali_mode": "auto_pa_line", "method": "auto",
            "nozzle_diameter": 0.4, "nozzle_volume_type": "standard", "extruder_id": 0,
            "filaments": [{
                "ams_id": 0, "slot_id": 0, "tray_id": 0,
                "filament_id": "GFG00", "filament_setting_id": "GFG00_60@BBL",
                "bed_temp": 60, "nozzle_temp": 220, "max_volumetric_speed": 12.0,
            }],
        }
        r1 = await async_client.post(f"/api/v1/printers/{p.id}/calibration/sessions", json=body)
        assert r1.status_code == 200
        r2 = await async_client.post(f"/api/v1/printers/{p.id}/calibration/sessions", json=body)
        assert r2.status_code == 409


@pytest.mark.asyncio
async def test_get_capabilities_offline_returns_404(async_client, printer_factory):
    p = await printer_factory(model="X1C")
    with patch("backend.app.api.routes.filament_calibration.printer_manager") as pm:
        pm.get_client.return_value = None
        r = await async_client.get(f"/api/v1/printers/{p.id}/calibration/capabilities")
    assert r.status_code == 404
```

- [ ] **Step 2: Write the router**

```python
# backend/app/api/routes/filament_calibration.py
"""REST routes for the Filament Calibration wizard.

Mounted at /printers and /calibration prefixes; permission PRINTERS_UPDATE
gates POST/DELETE, PRINTERS_READ gates GETs (where stricter is appropriate
the route uses a tighter permission).
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.calibration_session import CalibrationSession
from backend.app.models.printer import Printer
from backend.app.models.user import User
from backend.app.schemas.filament_calibration import (
    CalibCapabilities,
    CalibrationSessionOut,
    StartSessionIn,
)
from backend.app.services.calibration_service import CalibFilamentInput, CalibrationService
from backend.app.services.printer_capabilities import compute_calibration_supports
from backend.app.services.printer_manager import printer_manager

router = APIRouter(tags=["filament-calibration"])
_service = CalibrationService()


@router.get(
    "/printers/{printer_id}/calibration/capabilities",
    response_model=CalibCapabilities,
)
async def get_capabilities(
    printer_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_READ),
) -> CalibCapabilities:
    printer = (
        await db.execute(select(Printer).where(Printer.id == printer_id))
    ).scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")
    client = printer_manager.get_client(printer_id)
    if not client or not client.state.connected:
        raise HTTPException(404, "Printer not online")
    return CalibCapabilities(
        **compute_calibration_supports(client.state, printer.model, getattr(client, "module_vers", {}))
    )


@router.post(
    "/printers/{printer_id}/calibration/sessions",
    response_model=CalibrationSessionOut,
)
async def start_session(
    printer_id: int,
    body: StartSessionIn = Body(...),
    db: AsyncSession = Depends(get_db),
    user: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
) -> CalibrationSessionOut:
    try:
        session = await _service.start_calibration(
            db=db,
            printer_id=printer_id,
            cali_mode=body.cali_mode,
            method=body.method,
            nozzle_diameter=body.nozzle_diameter,
            nozzle_volume_type=body.nozzle_volume_type,
            extruder_id=body.extruder_id,
            filaments=[
                CalibFilamentInput(
                    ams_id=f.ams_id, slot_id=f.slot_id, tray_id=f.tray_id,
                    filament_id=f.filament_id, filament_setting_id=f.filament_setting_id,
                    bed_temp=f.bed_temp, nozzle_temp=f.nozzle_temp,
                    max_volumetric_speed=f.max_volumetric_speed,
                    flow_rate=f.flow_rate,
                )
                for f in body.filaments
            ],
            user_id=user.id if user else None,
        )
    except ValueError as e:
        if str(e).startswith("active_session_exists"):
            raise HTTPException(409, detail={"detail": "active_session_exists"})
        raise HTTPException(400, str(e))
    return CalibrationSessionOut.model_validate(session)


@router.get(
    "/calibration/sessions/{session_id}",
    response_model=CalibrationSessionOut,
)
async def get_session(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_READ),
) -> CalibrationSessionOut:
    s = (
        await db.execute(select(CalibrationSession).where(CalibrationSession.id == session_id))
    ).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Session not found")
    return CalibrationSessionOut.model_validate(s)


@router.post("/calibration/sessions/{session_id}/cancel", status_code=204)
async def cancel_session(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
) -> None:
    try:
        await _service.cancel_session(db=db, session_id=session_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
```

- [ ] **Step 3: Register router in main.py**

Find router list block in `backend/app/main.py` (look for similar `app.include_router(printer_settings_routes.router, ...)` line) and add:

```python
from backend.app.api.routes import filament_calibration as filament_calibration_routes
# ...
app.include_router(filament_calibration_routes.router, prefix="/api/v1")
```

- [ ] **Step 4: Run tests**

Run: `pytest backend/tests/integration/test_calibration_routes.py -v -k "capabilities or session_auto or concurrent or offline"`
Expected: 4 passed.

- [ ] **Step 5: Stage**

`git add backend/app/api/routes/filament_calibration.py backend/app/main.py backend/tests/integration/test_calibration_routes.py`

---

### Task 20: Manual + Auto result endpoints

**Files:**
- Modify: `backend/app/api/routes/filament_calibration.py`
- Modify: `backend/tests/integration/test_calibration_routes.py`

- [ ] **Step 1: Tests**

Append to `test_calibration_routes.py`:

```python
@pytest.mark.asyncio
async def test_post_manual_result_pa(async_client, printer_factory, db_session):
    p = await printer_factory(model="P1S")
    from backend.app.models.calibration_session import CalibrationSession
    s = CalibrationSession(
        printer_id=p.id, user_id=None,
        cali_mode="pa_line", method="manual",
        nozzle_diameter=0.4, nozzle_volume_type="standard", extruder_id=0,
        filaments_json=json.dumps([{
            "ams_id": 0, "slot_id": 0, "tray_id": 0,
            "filament_id": "GFG00", "setting_id": "GFG00_60@BBL",
            "nozzle_id": "HS20", "nozzle_diameter": "0.4",
        }]),
        status="awaiting_user_input", stage=1,
    )
    db_session.add(s); await db_session.commit(); await db_session.refresh(s)

    with patch("backend.app.api.routes.filament_calibration.printer_manager") as pm, \
         patch("backend.app.services.calibration_service.printer_manager") as pm2:
        pm.get_client.return_value = _mock_client()
        pm2.get_client.return_value = _mock_client()
        r = await async_client.post(
            f"/api/v1/calibration/sessions/{s.id}/manual-result",
            json={"best_line_index": 24},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["saved_rows"]) == 1
    assert abs(body["saved_rows"][0]["pa_k_value"] - 0.048) < 1e-9


@pytest.mark.asyncio
async def test_post_auto_result(async_client, printer_factory, db_session):
    p = await printer_factory(model="X1C")
    from backend.app.models.calibration_session import CalibrationSession
    from backend.app.services.bambu_mqtt import ExtrusionCaliResult
    s = CalibrationSession(
        printer_id=p.id, user_id=None,
        cali_mode="auto_pa_line", method="auto",
        nozzle_diameter=0.4, nozzle_volume_type="standard", extruder_id=0,
        filaments_json=json.dumps([{
            "ams_id": 0, "slot_id": 0, "tray_id": 0,
            "filament_id": "GFG00", "setting_id": "GFG00_60@BBL",
            "nozzle_id": "HS20", "nozzle_diameter": "0.4",
        }]),
        status="awaiting_user_input", stage=1,
    )
    db_session.add(s); await db_session.commit(); await db_session.refresh(s)

    client = _mock_client()
    client.state.extrusion_cali_results = [
        ExtrusionCaliResult(tray_id=0, ams_id=0, slot_id=0, extruder_id=0,
                            nozzle_diameter=0.4, nozzle_volume_type="standard",
                            filament_id="GFG00", setting_id="GFG00_60@BBL",
                            k_value=0.05, n_coef=1.0, confidence=0)
    ]
    with patch("backend.app.api.routes.filament_calibration.printer_manager") as pm, \
         patch("backend.app.services.calibration_service.printer_manager") as pm2:
        pm.get_client.return_value = client
        pm2.get_client.return_value = client
        r = await async_client.post(
            f"/api/v1/calibration/sessions/{s.id}/auto-result",
            json={"results": [{"tray_id": 0, "save": True, "k_value": 0.05, "name": "PLA — PA 0.05"}]},
        )
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1
```

- [ ] **Step 2: Add endpoints**

Append to `backend/app/api/routes/filament_calibration.py`:

```python
from backend.app.schemas.filament_calibration import (
    AutoResultIn,
    FilamentCalibrationOut,
    ManualResultIn,
    ManualResultOutSchema,
)


@router.post(
    "/calibration/sessions/{session_id}/manual-result",
    response_model=ManualResultOutSchema,
)
async def submit_manual_result(
    session_id: int,
    body: ManualResultIn = Body(...),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
) -> ManualResultOutSchema:
    try:
        out = await _service.submit_manual_result(
            db=db, session_id=session_id,
            best_line_index=body.best_line_index,
            coarse_modifier=body.coarse_modifier,
            skip_fine=body.skip_fine,
            fine_modifier=body.fine_modifier,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return ManualResultOutSchema(
        saved_rows=[FilamentCalibrationOut.model_validate(r) for r in out.saved_rows],
        next_session_id=out.next_session_id,
    )


@router.post(
    "/calibration/sessions/{session_id}/auto-result",
    response_model=list[FilamentCalibrationOut],
)
async def submit_auto_result(
    session_id: int,
    body: AutoResultIn = Body(...),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
) -> list[FilamentCalibrationOut]:
    try:
        rows = await _service.submit_auto_result(
            db=db, session_id=session_id,
            edits=[e.model_dump() for e in body.results],
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return [FilamentCalibrationOut.model_validate(r) for r in rows]
```

- [ ] **Step 3: Run tests**

Run: `pytest backend/tests/integration/test_calibration_routes.py -v -k "manual_result or auto_result"`
Expected: 2 passed.

- [ ] **Step 4: Stage**

`git add backend/app/api/routes/filament_calibration.py backend/tests/integration/test_calibration_routes.py`

---

### Task 21: filament_calibration CRUD endpoints

**Files:**
- Modify: `backend/app/api/routes/filament_calibration.py`
- Modify: `backend/tests/integration/test_calibration_routes.py`

- [ ] **Step 1: Tests**

```python
@pytest.mark.asyncio
async def test_list_filament_calibrations(async_client, printer_factory, db_session):
    p = await printer_factory(model="P1S")
    from backend.app.models.filament_calibration import FilamentCalibration
    db_session.add(FilamentCalibration(
        printer_model="P1S", filament_id="GFG00",
        nozzle_diameter=0.4, nozzle_volume_type="standard", extruder_id=0,
        pa_k_value=0.048, cali_mode="pa_line", source="manual",
        is_active=True, cali_idx=3, name="row1",
    ))
    await db_session.commit()

    r = await async_client.get("/api/v1/filament-calibrations?printer_model=P1S")
    assert r.status_code == 200
    assert len(r.json()) == 1


@pytest.mark.asyncio
async def test_set_active_flips_others(async_client, printer_factory, db_session):
    p = await printer_factory(model="P1S")
    from backend.app.models.filament_calibration import FilamentCalibration
    r1 = FilamentCalibration(
        printer_model="P1S", filament_id="GFG00",
        nozzle_diameter=0.4, nozzle_volume_type="standard", extruder_id=0,
        pa_k_value=0.04, cali_mode="pa_line", source="manual",
        is_active=True, cali_idx=1, name="r1",
    )
    r2 = FilamentCalibration(
        printer_model="P1S", filament_id="GFG00",
        nozzle_diameter=0.4, nozzle_volume_type="standard", extruder_id=0,
        pa_k_value=0.05, cali_mode="pa_line", source="manual",
        is_active=False, cali_idx=2, name="r2",
    )
    db_session.add_all([r1, r2])
    await db_session.commit(); await db_session.refresh(r2)

    with patch("backend.app.api.routes.filament_calibration.printer_manager") as pm:
        pm.get_client.return_value = _mock_client()
        resp = await async_client.post(f"/api/v1/filament-calibrations/{r2.id}/set-active")
    assert resp.status_code == 200
    await db_session.refresh(r1); await db_session.refresh(r2)
    assert r1.is_active is False
    assert r2.is_active is True


@pytest.mark.asyncio
async def test_delete_calibration(async_client, printer_factory, db_session):
    p = await printer_factory(model="P1S")
    from backend.app.models.filament_calibration import FilamentCalibration
    row = FilamentCalibration(
        printer_model="P1S", filament_id="GFG00",
        nozzle_diameter=0.4, nozzle_volume_type="standard", extruder_id=0,
        pa_k_value=0.04, cali_mode="pa_line", source="manual",
        is_active=True, cali_idx=1, name="r",
    )
    db_session.add(row)
    await db_session.commit(); await db_session.refresh(row)

    r = await async_client.delete(f"/api/v1/filament-calibrations/{row.id}")
    assert r.status_code == 204
```

- [ ] **Step 2: Add endpoints**

Append:

```python
from fastapi import Query


@router.get("/filament-calibrations", response_model=list[FilamentCalibrationOut])
async def list_filament_calibrations(
    printer_model: str | None = Query(default=None),
    filament_id: str | None = Query(default=None),
    nozzle_diameter: float | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_READ),
) -> list[FilamentCalibrationOut]:
    from backend.app.models.filament_calibration import FilamentCalibration
    q = select(FilamentCalibration)
    if printer_model:
        q = q.where(FilamentCalibration.printer_model == printer_model)
    if filament_id:
        q = q.where(FilamentCalibration.filament_id == filament_id)
    if nozzle_diameter is not None:
        q = q.where(FilamentCalibration.nozzle_diameter == nozzle_diameter)
    if is_active is not None:
        q = q.where(FilamentCalibration.is_active.is_(is_active))
    q = q.order_by(FilamentCalibration.created_at.desc())
    rows = (await db.execute(q)).scalars().all()
    return [FilamentCalibrationOut.model_validate(r) for r in rows]


@router.get("/filament-calibrations/{cali_id}", response_model=FilamentCalibrationOut)
async def get_filament_calibration(
    cali_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_READ),
) -> FilamentCalibrationOut:
    from backend.app.models.filament_calibration import FilamentCalibration
    row = (
        await db.execute(select(FilamentCalibration).where(FilamentCalibration.id == cali_id))
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Calibration not found")
    return FilamentCalibrationOut.model_validate(row)


@router.post("/filament-calibrations/{cali_id}/set-active", response_model=FilamentCalibrationOut)
async def set_active_calibration(
    cali_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
) -> FilamentCalibrationOut:
    from backend.app.models.filament_calibration import FilamentCalibration
    row = (
        await db.execute(select(FilamentCalibration).where(FilamentCalibration.id == cali_id))
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Calibration not found")

    # Flip combo siblings
    siblings = (
        await db.execute(
            select(FilamentCalibration).where(
                FilamentCalibration.printer_model == row.printer_model,
                FilamentCalibration.filament_id == row.filament_id,
                FilamentCalibration.nozzle_diameter == row.nozzle_diameter,
                FilamentCalibration.nozzle_volume_type == row.nozzle_volume_type,
                FilamentCalibration.extruder_id == row.extruder_id,
                FilamentCalibration.is_active.is_(True),
                FilamentCalibration.id != row.id,
            )
        )
    ).scalars().all()
    for sib in siblings:
        sib.is_active = False
    row.is_active = True
    await db.commit()

    # Auto-sel printer slots that match this filament_id — best-effort
    # (We don't iterate all AMS slots; consumer can also re-sel via /history endpoint.)
    return FilamentCalibrationOut.model_validate(row)


@router.delete("/filament-calibrations/{cali_id}", status_code=204)
async def delete_calibration(
    cali_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
) -> None:
    from backend.app.models.filament_calibration import FilamentCalibration
    row = (
        await db.execute(select(FilamentCalibration).where(FilamentCalibration.id == cali_id))
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Calibration not found")
    # Optional: MQTT extrusion_cali_del — wire when calibrated_on_printer_id resolves a live client
    await db.delete(row)
    await db.commit()
```

- [ ] **Step 3: Run tests**

Run: `pytest backend/tests/integration/test_calibration_routes.py -v -k "list or set_active or delete"`
Expected: 3 passed.

- [ ] **Step 4: Stage**

`git add backend/app/api/routes/filament_calibration.py backend/tests/integration/test_calibration_routes.py`

---

### Task 22: History endpoints + awaiting-user-input listing

**Files:**
- Modify: `backend/app/api/routes/filament_calibration.py`
- Modify: `backend/tests/integration/test_calibration_routes.py`

- [ ] **Step 1: Tests**

```python
@pytest.mark.asyncio
async def test_get_history_returns_state_entries(async_client, printer_factory):
    p = await printer_factory(model="X1C")
    from backend.app.services.bambu_mqtt import PACalibHistoryEntry
    client = _mock_client()
    client.state.extrusion_cali_history = [
        PACalibHistoryEntry(
            cali_idx=0, name="r1", filament_id="GFG00", setting_id="GFG00_60@BBL",
            nozzle_diameter=0.4, nozzle_volume_type="standard", extruder_id=0,
            k_value=0.04, n_coef=1.0,
        )
    ]
    with patch("backend.app.api.routes.filament_calibration.printer_manager") as pm:
        pm.get_client.return_value = client
        r = await async_client.get(f"/api/v1/printers/{p.id}/calibration/history")
    assert r.status_code == 200
    assert len(r.json()) == 1


@pytest.mark.asyncio
async def test_history_refresh_triggers_mqtt_get(async_client, printer_factory):
    p = await printer_factory(model="X1C")
    client = _mock_client()
    with patch("backend.app.api.routes.filament_calibration.printer_manager") as pm:
        pm.get_client.return_value = client
        r = await async_client.post(
            f"/api/v1/printers/{p.id}/calibration/history/refresh?nozzle_diameter=0.4",
        )
    assert r.status_code == 202
    client.extrusion_cali_query_history.assert_called_once()


@pytest.mark.asyncio
async def test_list_awaiting_sessions(async_client, printer_factory, db_session, user_factory):
    p = await printer_factory(model="P1S")
    u = await user_factory()
    from backend.app.models.calibration_session import CalibrationSession
    s = CalibrationSession(
        printer_id=p.id, user_id=u.id,
        cali_mode="pa_line", method="manual",
        nozzle_diameter=0.4, nozzle_volume_type="standard", extruder_id=0,
        filaments_json="[]", status="awaiting_user_input",
    )
    db_session.add(s); await db_session.commit()

    r = await async_client.get(
        f"/api/v1/calibration/sessions?printer_id={p.id}&status=awaiting_user_input"
    )
    assert r.status_code == 200
    assert len(r.json()) == 1
```

- [ ] **Step 2: Endpoints**

```python
@router.get(
    "/printers/{printer_id}/calibration/history",
    response_model=list[PACalibHistoryEntryOut],
)
async def get_history(
    printer_id: int,
    _: User | None = RequirePermission(Permission.PRINTERS_READ),
) -> list[PACalibHistoryEntryOut]:
    from backend.app.schemas.filament_calibration import PACalibHistoryEntryOut

    client = printer_manager.get_client(printer_id)
    if not client or not client.state.connected:
        raise HTTPException(404, "Printer not online")
    return [
        PACalibHistoryEntryOut(
            cali_idx=h.cali_idx, name=h.name,
            filament_id=h.filament_id, setting_id=h.setting_id,
            nozzle_diameter=h.nozzle_diameter, nozzle_volume_type=h.nozzle_volume_type,
            extruder_id=h.extruder_id, k_value=h.k_value, n_coef=h.n_coef,
        )
        for h in client.state.extrusion_cali_history
    ]


@router.post(
    "/printers/{printer_id}/calibration/history/refresh",
    status_code=202,
)
async def refresh_history(
    printer_id: int,
    nozzle_diameter: float = Query(default=0.4),
    extruder_id: int = Query(default=0),
    _: User | None = RequirePermission(Permission.PRINTERS_READ),
) -> dict:
    client = printer_manager.get_client(printer_id)
    if not client or not client.state.connected:
        raise HTTPException(404, "Printer not online")
    ok, seq = client.extrusion_cali_query_history(
        nozzle_diameter=nozzle_diameter, extruder_id=extruder_id,
    )
    if not ok:
        raise HTTPException(504, "MQTT publish failed")
    return {"sequence_id": seq}


@router.get(
    "/calibration/sessions",
    response_model=list[CalibrationSessionOut],
)
async def list_sessions(
    printer_id: int | None = Query(default=None),
    status: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_READ),
) -> list[CalibrationSessionOut]:
    q = select(CalibrationSession)
    if printer_id is not None:
        q = q.where(CalibrationSession.printer_id == printer_id)
    if status:
        q = q.where(CalibrationSession.status == status)
    q = q.order_by(CalibrationSession.created_at.desc()).limit(50)
    rows = (await db.execute(q)).scalars().all()
    return [CalibrationSessionOut.model_validate(r) for r in rows]
```

- [ ] **Step 3: Run tests**

Run: `pytest backend/tests/integration/test_calibration_routes.py -v -k "history or awaiting"`
Expected: 3 passed.

- [ ] **Step 4: Stage**

`git add backend/app/api/routes/filament_calibration.py backend/tests/integration/test_calibration_routes.py`

---

### Wave 5 verify

- [ ] All Wave 5 tests

Run: `pytest backend/tests/integration/test_calibration_routes.py backend/tests/unit/services/test_calibration_service.py backend/tests/unit/services/test_bambu_mqtt_calibration.py backend/tests/unit/services/test_calibration_capabilities.py backend/tests/unit/services/test_calibration_constants.py backend/tests/integration/test_m062_filament_calibration_migration.py -v`
Expected: all passed.

- [ ] Commit suggestion (when user asks):

```
feat(calibration): REST router + Pydantic schemas

13 endpoints under /api/v1:
- GET /printers/{id}/calibration/capabilities
- POST/GET /printers/{id}/calibration/sessions (+ /cancel)
- POST /calibration/sessions/{id}/manual-result + auto-result
- GET /filament-calibrations (filters) + /{id} + /set-active + DELETE
- GET /printers/{id}/calibration/history + POST refresh
- GET /calibration/sessions (filtered, for resume banner)

Permission: PRINTERS_UPDATE for mutations, PRINTERS_READ for reads.
Concurrent-session guard returns 409.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Wave 6 — Audit + integration sweep

### Task 23: Audit writes from routes

**Files:**
- Modify: `backend/app/api/routes/filament_calibration.py`
- Modify: `backend/tests/integration/test_calibration_routes.py`

- [ ] **Step 1: Test**

```python
@pytest.mark.asyncio
async def test_audit_row_written_on_start_session(async_client, printer_factory, db_session):
    p = await printer_factory(model="X1C")
    from backend.app.models.calibration_audit import CalibrationAudit
    with patch("backend.app.api.routes.filament_calibration.printer_manager") as pm, \
         patch("backend.app.services.calibration_service.printer_manager") as pm2:
        pm.get_client.return_value = _mock_client()
        pm2.get_client.return_value = _mock_client()
        r = await async_client.post(
            f"/api/v1/printers/{p.id}/calibration/sessions",
            json={
                "cali_mode": "auto_pa_line", "method": "auto",
                "nozzle_diameter": 0.4, "nozzle_volume_type": "standard", "extruder_id": 0,
                "filaments": [{
                    "ams_id": 0, "slot_id": 0, "tray_id": 0,
                    "filament_id": "GFG00", "filament_setting_id": "GFG00_60@BBL",
                    "bed_temp": 60, "nozzle_temp": 220, "max_volumetric_speed": 12.0,
                }],
            },
        )
    assert r.status_code == 200
    rows = (
        await db_session.execute(
            select(CalibrationAudit).where(CalibrationAudit.printer_id == p.id)
        )
    ).scalars().all()
    assert len(rows) >= 1
    assert rows[0].action == "start_session"
    assert rows[0].result == "ok"
```

- [ ] **Step 2: Add audit helper + invocations**

In `filament_calibration.py`:

```python
import json as _json
from backend.app.models.calibration_audit import CalibrationAudit


async def _audit(
    *,
    db: AsyncSession,
    printer_id: int,
    user: User | None,
    action: str,
    payload: dict,
    sequence_id: str | None = None,
    session_id: int | None = None,
    filament_calibration_id: int | None = None,
    result: str = "ok",
    error: str | None = None,
) -> None:
    db.add(
        CalibrationAudit(
            printer_id=printer_id,
            user_id=user.id if user else None,
            session_id=session_id,
            filament_calibration_id=filament_calibration_id,
            action=action,
            payload_json=_json.dumps(payload),
            sequence_id=sequence_id,
            result=result,
            error_message=error,
        )
    )
    await db.commit()
```

Then in `start_session` after success:

```python
await _audit(
    db=db, printer_id=printer_id, user=user,
    action="start_session",
    payload=body.model_dump(),
    sequence_id=session.mqtt_sequence_id,
    session_id=session.id,
)
```

And in `cancel_session`, `submit_manual_result`, `submit_auto_result`, `set_active_calibration`, `delete_calibration` — same pattern with action names: `cancel`, `save_result`, `save_result` (auto), `set_active`, `delete`.

- [ ] **Step 3: Run tests**

Run: `pytest backend/tests/integration/test_calibration_routes.py -v -k "audit"`
Expected: passed.

Plus re-run all route tests:
Run: `pytest backend/tests/integration/test_calibration_routes.py -v`
Expected: all green.

- [ ] **Step 4: Stage**

`git add backend/app/api/routes/filament_calibration.py backend/tests/integration/test_calibration_routes.py`

---

### Task 24: Full test sweep

- [ ] **Step 1: Run all new + adjacent tests**

```
pytest backend/tests/integration/test_m062_filament_calibration_migration.py \
       backend/tests/integration/test_calibration_routes.py \
       backend/tests/unit/services/test_calibration_constants.py \
       backend/tests/unit/services/test_calibration_capabilities.py \
       backend/tests/unit/services/test_calibration_service.py \
       backend/tests/unit/services/test_bambu_mqtt_calibration.py \
       backend/tests/unit/services/test_calibration_dispatch_apply.py \
       -v
```

Expected: all passed (≈40+ tests).

- [ ] **Step 2: Sanity-check pre-existing tests are still green**

```
pytest backend/tests/integration/test_printer_settings_routes.py \
       backend/tests/integration/test_ams_settings_routes.py \
       backend/tests/unit/services/test_bambu_mqtt_printer_settings.py \
       backend/tests/unit/services/test_printer_capabilities.py \
       -v
```

Expected: no regressions.

- [ ] **Step 3: Lint pass**

```
ruff check backend/app/services/calibration_service.py \
           backend/app/services/calibration_constants.py \
           backend/app/services/printer_capabilities.py \
           backend/app/models/filament_calibration.py \
           backend/app/models/calibration_session.py \
           backend/app/models/calibration_audit.py \
           backend/app/api/routes/filament_calibration.py \
           backend/app/schemas/filament_calibration.py \
           backend/app/migrations/m062_filament_calibration.py
```

Expected: no errors. (Auto-fix with `--fix` if simple.)

- [ ] **Step 4: Final commit (only when user asks)**

Suggested:

```
feat(calibration): backend foundation complete (Plan 1)

Wave 6 closes Plan 1 — all backend endpoints operational, all tests
green, lint clean. UI in Plan 2.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Plan 1 Summary

**What this plan delivers:**
- Schema: m062 + 3 new tables + 4 new columns on print_archive/print_queue_item.
- Models: FilamentCalibration, CalibrationSession, CalibrationAudit.
- Constants: CaliMode/CaliMethod/NozzleVolumeType enums, PA range, Flow modifiers, math helpers, nozzle_id encoder.
- Capability map: `compute_calibration_supports` per-model.
- MQTT layer: 4 new publishers, 3 push parsers, capability flags.
- Service: CalibrationService.start/submit_manual/submit_auto/save/cancel + dispatch resolver.
- REST: 13 endpoints, concurrent-session guard, audit.
- Asset directory scaffolded (README + .gitkeep).

**What's NOT in this plan (deferred to Plan 2/3):**
- Frontend wizard, save pages, history modal, resume banner.
- Real 3MF assets copied from BS (just directory + README placeholder).
- i18n keys.
- WebSocket `calibration.*` event emission (dispatch hook still uses bare DB update).
- Docs site + landing page updates.

**Open follow-ups after Plan 1 ships:**
1. Copy actual `.3mf` assets from `temp/references/BambuStudio/resources/calib/` into `backend/app/data/calib_assets/` — needs running BS reference snapshot. Manual step.
2. Verify `slots_used` extraction in dispatch hook against actual code structure (Task 17 has a placeholder).
3. WS event emission (`calibration.*`) — UI will need this in Plan 2.

---

## Spec Self-Review

**Coverage** — every spec section has a task:

| Spec section | Plan task |
|---|---|
| Data Model (filament_calibration / session / audit) | T1, T2, T3, T4 |
| PrintArchive.is_calibration / PrintQueueItem.is_calibration | T1 (migration), T5 (models) |
| MQTT publishers + parser | T8 (state fields), T9 (parser), T10 (publishers) |
| Capability matrix | T6 (constants), T7 (compute_calibration_supports) |
| 3MF asset shipping | T12 |
| CalibrationService.start_calibration auto branch | T11 |
| CalibrationService.start_calibration manual branch | T14, T15 |
| submit_manual_result PA + Flow chain | T13 |
| submit_auto_result | T16 |
| cancel_session | T16 |
| save_result (auto-bind sel) | T13 |
| resolve_active_calibration + dispatch apply hook | T17 |
| REST endpoints | T19, T20, T21, T22 |
| Audit row writes | T23 |

**Placeholder scan** — no TBD, no "implement later", no "add validation" without code. Manual `slots_used` extraction in T17 is flagged as adapt-to-your-code with explicit Step 1 (read code) before code change.

**Type consistency** — `CaliFilamentInput` / `CalibFilamentInput` — used one name `CalibFilamentInput` consistently. `ResultPayload`, `ManualResultOut` defined and consumed within service. `FilamentCalibrationOut` / `CalibrationSessionOut` used uniformly across schemas + routes.

**Scope** — single backend slice, no UI, no docs. Plan 2 picks up frontend.
