"""A pinned old collector and an explicitly upstream-shaped compatibility row.

The fixture came from the tagged collector on compatible current ORM rows,
not a historical schema replay. Tests also run in shallow Git checkouts.
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.services import meter_watch
from backend.tests.integration.test_git_restore_s2 import AT, restore

pytestmark = pytest.mark.asyncio


async def test_e15_tagged_collector_absent_energy_record_retains_source_reader(db_session):
    fixture = Path(__file__).parents[1] / "fixtures/git_restore_s2"
    provenance = json.loads((fixture / "v072-provenance.json").read_text())
    assert provenance["commit"] == "00e42a8a73972ac88be561ee3edb2cf71d83b8b5"
    rows = json.loads((fixture / "v072-energy.json").read_text())["archives"]
    assert all("energy_record" not in row and "recovery_identity" not in row for row in rows)
    await restore(db_session, rows)
    restored = {r.filename: r for r in (await db_session.scalars(select(PrintArchive))).all()}
    assert restored["old-measured.3mf"].energy_kwh == 0.42 and restored["old-measured.3mf"].energy_cost == 0.08
    assert meter_watch.energy_wire_fields(restored["old-measured.3mf"].extra_data, 0.42)["energy_status"] == "measured"
    assert meter_watch.energy_wire_fields(restored["old-no-plug.3mf"].extra_data, None)["energy_status"] is None


async def test_e15_upstream_shaped_missing_status_is_not_invented_success(db_session):
    # Minimal upstream-shaped legacy row: no UUID/serial/status authority.
    row = {"id": 1, "filename": "legacy.gcode", "started_at": str(AT), "file_path": "/foreign"}
    result = await restore(db_session, [row])
    assert result["archives"].skipped == 1 and not (await db_session.scalars(select(PrintArchive))).all()
