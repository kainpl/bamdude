"""No target DB needed to prove known historical source shapes stay accepted."""

import gzip
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from backend.app.core.schema_source_indexes import SourceIndexShapeError, validate_source_indexes


@pytest.mark.parametrize("fixture", ["v042", "v051", "v061", "pre075", "pre144"])
def test_actual_historical_producers_are_accepted_without_source_mutation(tmp_path, fixture):
    original = Path(__file__).resolve().parents[2] / "integration/fixtures/g02_pg_schema" / (fixture + ".db.gz")
    path = tmp_path / "copied.db"
    path.write_bytes(gzip.decompress(original.read_bytes()))
    before = path.read_bytes()
    validate_source_indexes(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("clause", ["batch_id DESC", "batch_id COLLATE NOCASE", "other"])
def test_current_key_mutation_is_rejected_before_import(tmp_path, clause):
    path = tmp_path / "copy.db"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE auto_queue_items(batch_id TEXT,other TEXT)")
        db.execute(f"CREATE INDEX ix_auto_queue_batch_id ON auto_queue_items({clause})")
        db.commit()
    with pytest.raises(SourceIndexShapeError):
        validate_source_indexes(path)


@pytest.mark.parametrize("level", [None, 62, 63, 230])
def test_only_pending_pre063_converter_can_own_the_old_calibration_key(tmp_path, level):
    path = tmp_path / "copy.db"
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "CREATE TABLE filament_calibration(filament_id INTEGER,nozzle_diameter REAL,nozzle_volume_type TEXT,extruder_id INTEGER,is_active BOOLEAN)"
        )
        db.execute(
            "CREATE UNIQUE INDEX ux_filament_cali_active ON filament_calibration(filament_id,nozzle_diameter,nozzle_volume_type,extruder_id) WHERE is_active = TRUE"
        )
        if level is not None:
            db.execute("CREATE TABLE _migrations(version INTEGER)")
            db.execute("INSERT INTO _migrations VALUES(?)", (level,))
        db.commit()
    if level is None or level < 63:
        validate_source_indexes(path)
    else:
        with pytest.raises(SourceIndexShapeError):
            validate_source_indexes(path)
