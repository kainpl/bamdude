"""D4 addressed PG data-policy gates, destructive only to TEST_POSTGRES_URL."""

import pytest
from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core import schema_conform_repair as repair
from backend.app.core.schema_repair_state import (
    RESIDUALS,
    SchemaRepairPending,
    finish_attempt,
    read_record,
    write_pending,
)
from backend.app.core.schema_residual_retry import retry_data_residuals
from backend.tests.integration.test_postgres_scenarios import (
    _async_url,
    _pg_url,
    _serialize_shared_postgres_target,  # noqa: F401
    _wipe,
)

pytestmark = pytest.mark.postgres


@pytest.fixture
async def conn():
    url = _pg_url()
    # _wipe uses its own asyncio.run; do not invoke it inside our running loop.
    import asyncio

    await asyncio.to_thread(_wipe, url, all_objects=True)
    engine = create_async_engine(_async_url(url))
    async with engine.begin() as connection:
        yield connection
    await engine.dispose()


async def test_r5_proves_missing_guards_preserves_rows_and_targeted_retry_enforces(conn):
    models = MetaData()
    probe = Table(
        "d4_probe",
        models,
        Column("id", Integer, primary_key=True),
        Column("required", String(10), nullable=False),
        Column("code", String(10)),
        Column("value", Integer),
        Column("indexed", String(10)),
        UniqueConstraint("code"),
        CheckConstraint("value >= 0", name="ck_d4_value"),
    )
    Index("ix_d4_unique", probe.c.indexed, unique=True)
    await conn.execute(
        text(
            "CREATE TABLE d4_probe(id SERIAL PRIMARY KEY,required VARCHAR(10),code VARCHAR(10),value INTEGER,indexed VARCHAR(10))"
        )
    )
    await conn.execute(
        text("INSERT INTO d4_probe(required,code,value,indexed) VALUES(NULL,'same',-1,'same'),(NULL,'same',-1,'same')")
    )
    before = (await conn.execute(text("SELECT * FROM d4_probe ORDER BY id"))).all()
    await repair.columns(conn, models)
    await repair.unique(conn, models)
    await repair.model_indexes(conn, models)
    await repair.checks(conn, models)
    proofs = await repair.verify(conn, models)
    assert len(proofs) == 4 and {p["reason"] for p in proofs} == {
        "null_rows",
        "duplicate_identity_groups",
        "check_rows",
    }
    assert (await conn.execute(text("SELECT * FROM d4_probe ORDER BY id"))).all() == before
    debt = await write_pending(conn, models)
    await finish_attempt(conn, debt, proofs)
    # Corrections belong to the domain/operator. Retry only restores guards.
    await conn.execute(text("UPDATE d4_probe SET required='known',code=id::text,value=1,indexed=id::text"))
    corrected = (await conn.execute(text("SELECT * FROM d4_probe ORDER BY id"))).all()
    await retry_data_residuals(conn, models)
    assert (await read_record(conn, RESIDUALS))["entries"] == {}
    assert await repair.verify(conn, models, fresh=True) == []
    assert (await conn.execute(text("SELECT * FROM d4_probe ORDER BY id"))).all() == corrected
    for statement in (
        "INSERT INTO d4_probe(required) VALUES(NULL)",
        "INSERT INTO d4_probe(required,code) VALUES('x','1')",
        "INSERT INTO d4_probe(required,value) VALUES('x',-1)",
        "INSERT INTO d4_probe(required,indexed) VALUES('x','1')",
    ):
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(text(statement))


@pytest.mark.parametrize("set_null", [False, True])
async def test_r7_preserves_existing_fk_detach_and_purge_before_classification(conn, set_null):
    models = MetaData()
    Table("d4_parent", models, Column("id", Integer, primary_key=True))
    Table(
        "d4_child",
        models,
        Column("id", Integer, primary_key=True),
        Column("parent_id", Integer, ForeignKey("d4_parent.id", ondelete="SET NULL" if set_null else "CASCADE")),
    )
    await conn.execute(text("CREATE TABLE d4_parent(id SERIAL PRIMARY KEY)"))
    await conn.execute(text("CREATE TABLE d4_child(id SERIAL PRIMARY KEY,parent_id INTEGER)"))
    await conn.execute(text("INSERT INTO d4_parent(id) VALUES(1)"))
    await conn.execute(text("INSERT INTO d4_child(id,parent_id) VALUES(1,1),(2,999)"))
    await repair.foreign_keys(conn, models)
    assert (await conn.execute(text("SELECT id,parent_id FROM d4_child ORDER BY id"))).all() == (
        [(1, 1), (2, None)] if set_null else [(1, 1)]
    )
    assert await repair.verify(conn, models, fresh=True) == []


async def test_unknown_check_definition_is_hard_not_a_data_residual(conn):
    models = MetaData()
    Table(
        "d4_probe",
        models,
        Column("id", Integer, primary_key=True),
        Column("value", Integer),
        CheckConstraint("value >= 0", name="ck_d4_value"),
    )
    await conn.execute(
        text("CREATE TABLE d4_probe(id SERIAL PRIMARY KEY,value INTEGER,CONSTRAINT ck_d4_value CHECK(value>-5))")
    )
    await conn.execute(text("INSERT INTO d4_probe(value) VALUES(-1)"))
    with pytest.raises(SchemaRepairPending, match="unverified_schema_objects"):
        await repair.verify(conn, models)


@pytest.mark.parametrize("partner", ["missing", "canonical", "approved", "renamed", "partial", "invalid", "unready"])
async def test_retained_missing_unique_requires_its_exact_active_partner(conn, partner):
    models = MetaData()
    Table(
        "kprofile_notes",
        models,
        Column("id", Integer, primary_key=True),
        Column("filament_calibration_id", Integer),
        UniqueConstraint("filament_calibration_id"),
    )
    await conn.execute(text("CREATE TABLE kprofile_notes(id SERIAL PRIMARY KEY,filament_calibration_id INTEGER)"))
    if partner == "canonical":
        await conn.execute(
            text(
                "ALTER TABLE kprofile_notes ADD CONSTRAINT kprofile_notes_filament_calibration_id_key UNIQUE(filament_calibration_id)"
            )
        )
    elif partner != "missing":
        name = "unapproved_partner" if partner == "renamed" else "uq_kprofile_notes_fc"
        predicate = " WHERE filament_calibration_id IS NOT NULL" if partner == "partial" else ""
        await conn.execute(text(f"CREATE UNIQUE INDEX {name} ON kprofile_notes(filament_calibration_id){predicate}"))
        if partner in ("invalid", "unready"):
            flag = "indisvalid" if partner == "invalid" else "indisready"
            await conn.execute(
                text(f"UPDATE pg_index SET {flag}=false WHERE indexrelid='uq_kprofile_notes_fc'::regclass")
            )
    if partner in ("approved", "canonical"):
        assert await repair.verify(conn, models) == []
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(text("INSERT INTO kprofile_notes(filament_calibration_id) VALUES(7),(7)"))
    else:
        with pytest.raises(SchemaRepairPending, match="unverified_schema_objects"):
            await repair.verify(conn, models)


async def test_portable_omissions_are_current_snapshot_proofs_not_stored_notes(conn):
    from backend.app.core.db_portable import _portable_metadata
    from backend.app.core.schema_portable_residuals import adapt_portable

    models = MetaData()
    Table("d4_probe", models, Column("id", Integer, primary_key=True), Column("value", Integer, nullable=False))
    await conn.execute(text("CREATE TABLE d4_probe(id SERIAL PRIMARY KEY,value INTEGER)"))
    await conn.execute(text("INSERT INTO d4_probe(value) VALUES(NULL)"))
    before = str(models.tables["d4_probe"].c.value.nullable)
    portable = _portable_metadata(models)
    proofs = await adapt_portable(conn, portable, models)
    assert len(proofs) == 1 and portable.tables["d4_probe"].c.value.nullable is True
    assert str(models.tables["d4_probe"].c.value.nullable) == before == "False"
    await conn.execute(text("UPDATE d4_probe SET value=1"))
    # A persisted residual would now be stale. It cannot justify omission.
    portable = _portable_metadata(models)
    assert await adapt_portable(conn, portable, models) == []
    assert portable.tables["d4_probe"].c.value.nullable is False


def canonical_check_cases():
    from backend.app.core.database import Base, import_all_models

    import_all_models()
    witnesses = {
        "ck_stock_items_reserved_le_on_hand": ({"reserved": 1, "on_hand": 0}, {"reserved": 0, "on_hand": 0}),
        "ck_ha_history_one_owner": (
            {"printer_sensor_id": None, "location_sensor_id": None},
            {"printer_sensor_id": 1, "location_sensor_id": None},
        ),
        "ck_auto_link_requires_verified_email_claim": (
            {"auto_link_existing_accounts": True, "email_claim": "email", "require_email_verified": False},
            {"auto_link_existing_accounts": False, "email_claim": "email", "require_email_verified": False},
        ),
        "ck_oidc_icon_triplet_co_null": (
            {"icon_data": b"x", "icon_content_type": None, "icon_etag": None},
            {"icon_data": None, "icon_content_type": None, "icon_etag": None},
        ),
        "ck_plate_renders_sha256": ({"file_sha256": ""}, {"file_sha256": "a" * 64}),
        "ck_plate_renders_status": ({"status": "bad"}, {"status": "pending"}),
        "ck_plate_renders_phase": ({"phase": "bad"}, {"phase": "render"}),
        "ck_plate_render_objects_method": ({"method": "bad"}, {"method": "toolpath"}),
        "ck_queue_sources_sha256_length": ({"sha256": ""}, {"sha256": "a" * 64}),
        "ck_queue_sources_format": ({"format": "bad"}, {"format": "3mf"}),
        "ck_queue_sources_state": ({"state": "bad"}, {"state": "ready"}),
        "ck_smart_sensor_binding_one_target": (
            {"printer_id": None, "printer_location_id": None, "storage_location_id": None},
            {"printer_id": 1, "printer_location_id": None, "storage_location_id": None},
        ),
        "ck_spoolman_kp_extruder_range": ({"extruder": 2}, {"extruder": 0}),
        "ck_spoolman_slot_ams_id_range": ({"ams_id": 8}, {"ams_id": 0}),
        "ck_spoolman_slot_tray_id_range": ({"tray_id": 4}, {"tray_id": 0}),
        "ck_stock_issue_lines_quantity": ({"quantity": 0}, {"quantity": 1}),
    }
    checks = [
        c
        for table in Base.metadata.tables.values()
        for c in table.constraints
        if isinstance(c, CheckConstraint) and c.name
    ]
    cases = []
    for con in checks:
        # The remaining canonical checks are the explicitly simple numeric
        # nonnegative counters; refuse a new expression without a witness.
        if con.name not in witnesses:
            import re

            match = re.fullmatch(r"([a-z_]+) >= 0", str(con.sqltext))
            assert match, (con.name, str(con.sqltext))
            witnesses[con.name] = ({match[1]: -1}, {match[1]: 0})
        cases.append((con, *witnesses[con.name]))
    assert {c.name for c in checks} == set(witnesses)
    return sorted(cases, key=lambda c: c[0].name)


@pytest.mark.parametrize(
    "definition,bad,good", canonical_check_cases(), ids=lambda v: v.name if isinstance(v, CheckConstraint) else None
)
async def test_each_canonical_named_check_has_a_real_data_residual_and_targeted_repair(conn, definition, bad, good):
    # Isolate only the relevant typed columns of the REAL model definition.
    # Other required fields/FKs must not mask this CHECK's data failure.
    models = MetaData()
    table = Table(
        definition.table.name,
        models,
        *(Column(name, definition.table.c[name].type, nullable=True) for name in bad),
        CheckConstraint(str(definition.sqltext), name=definition.name),
    )
    actual = Table(table.name, MetaData(), *(Column(name, definition.table.c[name].type) for name in bad))
    await conn.run_sync(lambda s: actual.create(s))
    await conn.execute(actual.insert().values(**bad))
    before = (await conn.execute(actual.select())).all()
    await repair.checks(conn, models)
    proofs = await repair.verify(conn, models)
    assert len(proofs) == 1 and proofs[0]["count"] == 1 and proofs[0]["reason"] == "check_rows"
    assert (await conn.execute(actual.select())).all() == before
    debt = await write_pending(conn, models)
    await finish_attempt(conn, debt, proofs)
    await conn.execute(actual.update().values(**good))
    await retry_data_residuals(conn, models)
    assert (await read_record(conn, RESIDUALS))["entries"] == {}
    assert await repair.verify(conn, models, fresh=True) == []
    with pytest.raises(IntegrityError) as caught:
        async with conn.begin_nested():
            await conn.execute(actual.insert().values(**bad))
    assert caught.value.orig.sqlstate == "23514"
