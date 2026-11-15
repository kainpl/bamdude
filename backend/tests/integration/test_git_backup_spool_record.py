"""The Git backup carries a spool's bookkeeping, and archived spools too
(upstream 71d4b2f70 leg 8 — G05 U23, Р33).

The export wrote 16 keys of active spools only. The restore read five more that
were never written (core weight, weight lock, data origin, tag type, archived
date), and neither side carried the category, the low-stock override, the
storage and purchase fields, the lot, the colours of a multi-colour spool or
the drying record — so our own backup, restored onto a clean install, gave back
uncategorised spools with a 250 g tare, and the history of an archived spool
restored onto nothing.
"""

import json
from datetime import datetime

import pytest
from sqlalchemy import delete, select

from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services.git_backup import GitBackupService
from backend.app.services.git_restore import GitRestoreService, _CategoryTally

BOOKKEEPING = {
    "category": "Production",
    "low_stock_threshold_pct": 35,
    "storage_location": "Shelf A4",
    "purchase_location": "Local shop",
    "purchase_date": datetime(2026, 4, 15, 0, 0, 0),
    "lot": 3,
    "material_number": "15",
    "core_weight": 180,
    "weight_locked": True,
    "weight_used_baseline": 120.0,
    "data_origin": "manual",
    "tag_type": "generic",
    "extra_colors": "FF0000,00FF00",
    "effect_type": "silk",
    "filament_diameter": "2.85",
    "last_dried_at": datetime(2026, 10, 6, 11, 5, 0),
    "last_dried_temp": 55,
    "last_dried_hours": 8.0,
}


def _spool(**fields) -> Spool:
    base = {
        "material": "PLA",
        "subtype": "Basic",
        "brand": "Acme",
        "color_name": "Black",
        "rgba": "000000FF",
        "label_weight": 1000,
        "weight_used": 200.0,
        "created_at": datetime(2026, 1, 2, 3, 4, 5),
    }
    return Spool(**{**base, **fields})


async def _backed_up(db) -> dict:
    files: dict = {}
    await GitBackupService()._collect_spools(db, files)
    return json.loads(json.dumps(files))  # what the provider stored: JSON text


async def _restore(db, files: dict, *, overwrite: bool = False) -> _CategoryTally:
    tally = _CategoryTally()
    await GitRestoreService()._restore_spools(
        db, files["spools/inventory.json"], files.get("spools/usage_history.json"), overwrite, tally, {}
    )
    await db.commit()
    return tally


async def _wipe(db) -> None:
    await db.execute(delete(SpoolUsageHistory))
    await db.execute(delete(Spool))
    await db.commit()
    db.expunge_all()


@pytest.mark.asyncio
@pytest.mark.integration
class TestRoundTrip:
    async def test_a_clean_install_gets_the_bookkeeping_back(self, db_session):
        db_session.add(_spool(**BOOKKEEPING))
        await db_session.commit()
        files = await _backed_up(db_session)
        await _wipe(db_session)

        await _restore(db_session, files)

        restored = (await db_session.execute(select(Spool))).scalars().one()
        for field, value in BOOKKEEPING.items():
            assert getattr(restored, field) == value, field
        assert restored.archived_at is None

    async def test_an_archived_spool_and_its_history_come_back(self, db_session):
        archived = _spool(color_name="White", archived_at=datetime(2026, 9, 1, 8, 0, 0), category="Old stock")
        db_session.add(archived)
        await db_session.flush()
        db_session.add(
            SpoolUsageHistory(spool_id=archived.id, weight_used=40.0, created_at=datetime(2026, 8, 30, 9, 0, 0))
        )
        await db_session.commit()
        files = await _backed_up(db_session)
        assert files["spools/inventory.json"]["spool_count"] == 1
        await _wipe(db_session)

        await _restore(db_session, files)

        restored = (await db_session.execute(select(Spool))).scalars().one()
        assert restored.archived_at == datetime(2026, 9, 1, 8, 0, 0)
        assert restored.category == "Old stock"
        history = (await db_session.execute(select(SpoolUsageHistory))).scalars().all()
        assert [(row.spool_id, row.weight_used) for row in history] == [(restored.id, 40.0)]

    async def test_overwrite_brings_a_changed_field_back(self, db_session):
        spool = _spool(**BOOKKEEPING)
        db_session.add(spool)
        await db_session.commit()
        files = await _backed_up(db_session)
        spool.category = "Changed since"
        spool.material_number = "99"
        spool.core_weight = 250
        await db_session.commit()

        await _restore(db_session, files, overwrite=True)

        await db_session.refresh(spool)
        assert (spool.category, spool.material_number, spool.core_weight) == ("Production", "15", 180)


@pytest.mark.asyncio
@pytest.mark.integration
class TestAnOlderBackup:
    """A backup written before these keys existed says nothing about them."""

    OLD_KEYS = (
        "id",
        "material",
        "subtype",
        "color_name",
        "rgba",
        "brand",
        "label_weight",
        "weight_used",
        "slicer_filament",
        "slicer_filament_name",
        "nozzle_temp_min",
        "nozzle_temp_max",
        "cost_per_kg",
        "note",
        "tag_uid",
        "tray_uuid",
        "created_at",
    )

    def _as_old(self, files: dict) -> dict:
        inventory = files["spools/inventory.json"]
        inventory["spools"] = [{k: entry[k] for k in self.OLD_KEYS} for entry in inventory["spools"]]
        return files

    async def test_overwrite_leaves_the_live_bookkeeping_alone(self, db_session):
        spool = _spool(**BOOKKEEPING)
        db_session.add(spool)
        await db_session.commit()
        files = self._as_old(await _backed_up(db_session))

        await _restore(db_session, files, overwrite=True)

        await db_session.refresh(spool)
        for field in (
            "category",
            "low_stock_threshold_pct",
            "storage_location",
            "purchase_location",
            "purchase_date",
            "lot",
            "material_number",
            "weight_used_baseline",
            "extra_colors",
            "effect_type",
            "filament_diameter",
            "last_dried_at",
            "last_dried_temp",
            "last_dried_hours",
        ):
            assert getattr(spool, field) == BOOKKEEPING[field], field

    async def test_a_clean_install_restores_as_before(self, db_session):
        db_session.add(_spool(**BOOKKEEPING))
        await db_session.commit()
        files = self._as_old(await _backed_up(db_session))
        await _wipe(db_session)

        tally = await _restore(db_session, files)

        restored = (await db_session.execute(select(Spool))).scalars().one()
        assert tally.restored == 1
        assert (restored.material, restored.brand, restored.weight_used) == ("PLA", "Acme", 200.0)
        assert (restored.core_weight, restored.category, restored.material_number) == (250, None, None)
