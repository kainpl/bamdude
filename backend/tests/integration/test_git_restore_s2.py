"""S2 contracts through real collectors, category owner and metadata writers."""

import json
from collections import Counter
from datetime import datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

from backend.app.models.archive import PrintArchive
from backend.app.models.printer import Printer
from backend.app.models.project import Project
from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.models.user import User
from backend.app.schemas.git_backup import RestoreCategory
from backend.app.services import meter_watch
from backend.app.services.git_backup import GitBackupService
from backend.app.services.git_restore import ARCHIVES_PATH, SPOOL_USAGE_PATH, SPOOLS_PATH, GitRestoreService

pytestmark = pytest.mark.asyncio
AT = datetime(2026, 10, 1, 12)
END = datetime(2026, 10, 1, 13)


async def archive(db, **fields):
    row = PrintArchive(
        **{
            "filename": "source.gcode",
            "file_path": "",
            "file_size": 0,
            "started_at": AT,
            "status": "completed",
            **fields,
        }
    )
    db.add(row)
    await db.commit()
    return row


async def printer(db, serial="P-A"):
    row = Printer(name="Synthetic", serial_number=serial, ip_address="127.0.0.1", access_code="probe")
    db.add(row)
    await db.commit()
    return row


def entry(**fields):
    return {"id": 91, "filename": "source.gcode", "started_at": AT.isoformat(), "status": "completed", **fields}


async def restore(db, entries, *, overwrite=True, service=None, results=None):
    return await (service or GitRestoreService())._apply(
        db, {ARCHIVES_PATH: {"archives": entries}}, [RestoreCategory.ARCHIVES], overwrite, results=results
    )


def params(tally, code="archivesInputSummary"):
    return next(n["params"] for n in tally.notes if n["code"] == code)


async def collect(db):
    files = {}
    await GitBackupService()._collect_archives(db, files)
    await GitBackupService()._collect_spools(db, files)
    return json.loads(json.dumps(files))


async def test_e01_collector_carries_execution_and_link_metadata(db_session):
    p = await printer(db_session)
    a = await archive(
        db_session,
        printer_id=p.id,
        failure_reason="userCancelled",
        bed_temperature=60,
        nozzle_temperature=210,
        external_url="https://example.com/source",
        extra_data={"recovered_outcome_uncertain": True, "do_not_copy": "private"},
    )
    s = Spool(material="PLA", tag_uid="SPOOL")
    db_session.add(s)
    await db_session.flush()
    db_session.add(SpoolUsageHistory(spool_id=s.id, archive_id=a.id, printer_id=p.id, weight_used=12, created_at=AT))
    await db_session.commit()
    files = await collect(db_session)
    row = files[ARCHIVES_PATH]["archives"][0]
    assert row["recovery_identity"] == a.recovery_identity and row["printer_serial"] == p.serial_number
    assert row["failure_reason"] == "userCancelled" and row["bed_temperature"] == 60
    assert row["nozzle_temperature"] == 210 and row["external_url"] == "https://example.com/source"
    assert row["recovered_outcome_uncertain"] is True and "extra_data" not in row and "do_not_copy" not in row
    usage = files[SPOOL_USAGE_PATH]["usage_history"][0]
    assert usage["archive_id"] == a.id and usage["printer_serial"] == p.serial_number


async def test_e02_uuid_match_survives_attach_rename_and_patch(db_session):
    a = await archive(db_session, content_hash="a" * 64, file_size=400)
    backup = (await collect(db_session))[ARCHIVES_PATH]["archives"]
    a.filename, a.file_path, a.content_hash, a.total_layers = "attached.3mf", "owned/attached.3mf", "b" * 64, 693
    await db_session.commit()
    # A source snapshot from the active print is not allowed to undo late attach.
    backup[0]["status"] = "printing"
    backup[0]["notes"] = "operator note"
    result = await restore(db_session, backup)
    await db_session.refresh(a)
    assert await db_session.scalar(select(func.count()).select_from(PrintArchive)) == 1
    assert (a.filename, a.file_path, a.file_size, a.content_hash, a.total_layers) == (
        "attached.3mf",
        "owned/attached.3mf",
        400,
        "b" * 64,
        693,
    )
    assert a.notes == "operator note" and result["archives"].restored == 1


@pytest.mark.parametrize("bad", ["", "not-uuid", True, 7, "1" * 32])
async def test_e03_explicit_bad_uuid_never_uses_name_fallback(db_session, bad):
    a = await archive(db_session, notes="keep")
    result = await restore(db_session, [entry(recovery_identity=bad, notes="wrong")])
    await db_session.refresh(a)
    assert a.notes == "keep" and result["archives"].skipped == 1


async def test_e03_duplicate_uuid_and_old_id_preflight(db_session):
    key = str(uuid4())
    rows = [
        entry(id=1, recovery_identity=key),
        entry(id=2, recovery_identity=key.upper()),
        entry(id=3, recovery_identity=str(uuid4())),
        entry(id=3, recovery_identity=str(uuid4())),
        "bad",
    ]
    result = (await restore(db_session, rows))["archives"]
    assert (result.restored, result.skipped, result.failed) == (0, 2, 3)
    assert await db_session.scalar(select(func.count()).select_from(PrintArchive)) == 0


@pytest.mark.parametrize("case", ["null", "unproven", "same", "other"])
async def test_e04_legacy_minted_twin_is_conflict_only(db_session, case):
    p = await printer(db_session, "P-A")
    q = await printer(db_session, "P-B")
    local = await archive(
        db_session,
        printer_id=None if case == "null" else q.id if case == "other" else p.id,
        deleted_at=END,
        notes="keep",
    )
    row = entry(
        recovery_identity=str(uuid4()),
        printer_id=p.id,
        printer_serial="WRONG" if case == "unproven" else "P-A",
        notes="incoming",
    )
    first = (await restore(db_session, [row]))["archives"]
    second = (await restore(db_session, [row]))["archives"]
    if case == "other":
        assert first.restored == 1 and await db_session.scalar(select(func.count()).select_from(PrintArchive)) == 2
    else:
        assert first.skipped == second.skipped == 1
        assert await db_session.scalar(select(func.count()).select_from(PrintArchive)) == 1
        await db_session.refresh(local)
        assert local.notes == "keep" and local.deleted_at == END


async def test_e05_legacy_cardinality_and_source_artifact_count(db_session):
    await archive(db_session)
    await archive(db_session)
    result = (await restore(db_session, [entry(), entry(id=92, filename="reslice.3mf", started_at=None)]))["archives"]
    assert result.skipped == 2 and result.restored == 0
    assert params(result)["legacy_source_artifacts_skipped"] == 1


@pytest.mark.parametrize("status", ["printing", "paused", "opaque", None, True])
async def test_e06_unknown_new_execution_is_history_only(db_session, monkeypatch, status):
    p = await printer(db_session)
    import backend.app.services.git_restore as module

    manager = Mock()
    monkeypatch.setattr(module, "printer_manager", manager)
    result = await restore(
        db_session,
        [
            entry(
                recovery_identity=str(uuid4()),
                printer_id=p.id,
                printer_serial="P-A",
                status=status,
                completed_at=END.isoformat(),
                print_time_seconds=3600,
                filament_used_grams=100,
                cost=10,
                energy_kwh=0.5,
                energy_cost=0.1,
                energy_record={},
                quantity=0,
            )
        ],
    )
    row = await db_session.scalar(select(PrintArchive))
    assert row.status == "cancelled" and row.failure_reason == "noStatusUpdate"
    assert row.extra_data["recovered_outcome_uncertain"] is True
    assert (row.completed_at, row.print_time_seconds, row.filament_used_grams, row.cost, row.energy_kwh) == (None,) * 5
    assert row.quantity == 0 and result["archives"].restored == 1
    assert meter_watch.energy_wire_fields(row.extra_data, row.energy_kwh)["energy_status"] == "unavailable"
    assert manager.method_calls == []


async def test_e06_legacy_inflight_or_unproven_new_inflight_skips(db_session):
    result = (
        await restore(
            db_session, [entry(status="printing"), entry(id=92, status="paused", recovery_identity=str(uuid4()))]
        )
    )["archives"]
    assert result.skipped == 2 and result.restored == 0


@pytest.mark.parametrize("status,path", [("completed", "owned.3mf"), ("printing", ""), ("paused", "owned.3mf")])
async def test_e09_o_mode_protects_every_column_and_domain_helper(db_session, monkeypatch, status, path):
    project = Project(name="Keep order")
    user = User(username="owner")
    db_session.add_all([project, user])
    await db_session.commit()
    a = await archive(
        db_session,
        status=status,
        file_path=path,
        file_size=500,
        project_id=project.id,
        created_by_id=user.id,
        completed_at=END if status == "completed" else None,
        total_layers=693,
        cost=9,
        energy_kwh=0.8,
        energy_cost=0.2,
        quantity=7,
        notes="old",
        is_favorite=True,
        extra_data={"unrelated": "keep", "energy_provenance": {"kind": "estimated", "coverage": 0.97}},
    )
    before = {
        col.name: getattr(a, col.name)
        for col in PrintArchive.__table__.columns
        if col.name not in {"notes", "tags", "is_favorite", "external_url"}
    }
    import backend.app.services.git_restore as module

    monkeypatch.setattr(module.swap_compatibility, "restored_declaration", Mock(side_effect=AssertionError("O helper")))
    monkeypatch.setattr(module, "owner_value", Mock(side_effect=AssertionError("O owner")))
    service = GitRestoreService()
    service._restore_source_declaration = AsyncMock(side_effect=AssertionError("O source helper"))
    row = entry(
        recovery_identity=a.recovery_identity,
        status="printing",
        print_name="stale",
        total_layers=0,
        quantity=1,
        cost=None,
        energy_kwh=0,
        energy_cost=0,
        energy_record={},
        project_id=None,
        created_by_id=None,
        deleted_at=None,
        notes="new",
        tags=None,
        is_favorite=False,
        external_url=None,
    )
    await restore(db_session, [row], service=service)
    await db_session.refresh(a)
    assert before == {key: getattr(a, key) for key in before}
    assert a.notes == "new" and a.is_favorite is False


@pytest.mark.parametrize("path", ["/unowned/source.3mf", "", None])
async def test_e11_fixed_path_and_insert_only_identity(db_session, path):
    row = entry(recovery_identity=str(uuid4()), file_path=path, file_size=0, quantity=0)
    await restore(db_session, [row])
    a = await db_session.scalar(select(PrintArchive))
    assert a.file_path == "" and a.quantity == 0
    a.file_path = "owned/keep.3mf"
    await db_session.commit()
    await restore(db_session, [{**row, "filename": "changed.3mf", "file_size": 999, "started_at": END.isoformat()}])
    await db_session.refresh(a)
    assert (a.file_path, a.filename, a.file_size, a.started_at) == ("owned/keep.3mf", "source.gcode", 0, AT)


async def test_e13_native_energy_class_and_sums_roundtrip(db_session):
    data = [
        (None, None, None),
        (0.42, 0.08, None),
        (0, 0, None),
        (0.3, 0.05, {"energy_is_approximate": True}),
        (0.2, 0.04, {"energy_provenance": {"kind": "estimated", "coverage": 0.97, "final": True}}),
        (None, None, {"energy_provenance": {"kind": "unavailable", "unavailable_reason": "insufficient_history"}}),
    ]
    for i, (kwh, cost, extra) in enumerate(data):
        await archive(db_session, filename=f"energy-{i}.3mf", energy_kwh=kwh, energy_cost=cost, extra_data=extra)
    before = [
        (r.filename, r.energy_kwh, r.energy_cost, meter_watch.energy_wire_fields(r.extra_data, r.energy_kwh))
        for r in (await db_session.scalars(select(PrintArchive).order_by(PrintArchive.filename)))
    ]
    files = await collect(db_session)
    await db_session.execute(delete(PrintArchive))
    await db_session.commit()
    await restore(db_session, files[ARCHIVES_PATH]["archives"])
    after = [
        (r.filename, r.energy_kwh, r.energy_cost, meter_watch.energy_wire_fields(r.extra_data, r.energy_kwh))
        for r in (await db_session.scalars(select(PrintArchive).order_by(PrintArchive.filename)))
    ]
    assert after == before


@pytest.mark.parametrize("username,owner", [([], 1), ("missing", 1), (None, True), ("", True)])
async def test_e16_no_malformed_name_or_bool_owner_fallback(db_session, username, owner):
    db_session.add(User(id=1, username="local"))
    await db_session.commit()
    await restore(
        db_session, [entry(recovery_identity=str(uuid4()), created_by_username=username, created_by_id=owner)]
    )
    assert (await db_session.scalar(select(PrintArchive))).created_by_id is None


async def test_e17_e18_actual_history_alias_counts_and_proven_link(db_session):
    p = await printer(db_session)
    a = await archive(db_session, printer_id=p.id)
    s = Spool(material="PLA", tag_uid="MAP-SPOOL", weight_used=200)
    db_session.add(s)
    await db_session.flush()
    db_session.add(SpoolUsageHistory(spool_id=s.id, archive_id=a.id, printer_id=p.id, weight_used=12, created_at=AT))
    await db_session.commit()
    files = await collect(db_session)
    await db_session.execute(delete(SpoolUsageHistory))
    await db_session.execute(delete(PrintArchive))
    await db_session.commit()
    service = GitRestoreService()
    result = await service._apply(db_session, files, [RestoreCategory.ARCHIVES, RestoreCategory.SPOOLS], False)
    usage = await db_session.scalar(select(SpoolUsageHistory))
    target = await db_session.scalar(select(PrintArchive))
    assert usage.archive_id == target.id and target.recovery_identity == a.recovery_identity
    assert usage.printer_id == p.id
    assert s.weight_used == 200
    count, _ = await service._count_items(db_session, RestoreCategory.SPOOLS, files)
    tally = result["spools"]
    assert tally.restored + tally.skipped + tally.failed == count == 2
    await db_session.commit()
    await service._apply(db_session, files, [RestoreCategory.ARCHIVES, RestoreCategory.SPOOLS], False)
    assert await db_session.scalar(select(func.count()).select_from(SpoolUsageHistory)) == 1


async def test_e19_unexpected_db_failure_rolls_back_current_category(db_session):
    await db_session.execute(
        text(
            "CREATE TRIGGER fail_archive BEFORE INSERT ON print_archives WHEN NEW.filename='FAIL' BEGIN SELECT RAISE(ABORT, 'probe'); END"
        )
    )
    await db_session.commit()
    results = {}
    with pytest.raises(IntegrityError):
        await restore(
            db_session,
            [entry(id=1, recovery_identity=str(uuid4())), entry(id=2, filename="FAIL", recovery_identity=str(uuid4()))],
            results=results,
        )
    await db_session.rollback()
    assert results == {} and await db_session.scalar(select(func.count()).select_from(PrintArchive)) == 0


@pytest.mark.parametrize("supplied", [None, True, 99999, "1", 1.0])
async def test_e07_printer_needs_exact_id_and_serial(db_session, supplied):
    p = await printer(db_session)
    result = await restore(db_session, [entry(printer_id=supplied, printer_serial=p.serial_number)])
    row = (await db_session.scalars(select(PrintArchive))).one()
    assert row.printer_id is None and result["archives"].restored == 1


async def test_e07_deleted_printer_id_reused_by_another_serial_is_not_authority(db_session):
    p = await printer(db_session, "OLD")
    key = p.id
    await db_session.execute(delete(Printer).where(Printer.id == key))
    await db_session.commit()
    db_session.add(
        Printer(id=key, name="Replacement", serial_number="NEW", ip_address="127.0.0.1", access_code="probe")
    )
    await db_session.commit()
    result = (await restore(db_session, [entry(printer_id=key, printer_serial="OLD")]))["archives"]
    row = (await db_session.scalars(select(PrintArchive))).one()
    assert row.printer_id is None and any(n["code"] == "archivesPrinterUnproven" for n in result.notes)


@pytest.mark.parametrize("status", ["aborted", "stopped", "skipped", "archived", "cancelled", "failed"])
async def test_e08_supported_terminal_history_is_preserved(db_session, status):
    await restore(db_session, [entry(status=status)])
    assert (await db_session.scalars(select(PrintArchive))).one().status == status


async def test_e10_local_uncertainty_and_absent_operator_fields_skip_but_map(db_session):
    from backend.app.services.git_restore import _CategoryTally

    a = await archive(db_session, extra_data={"recovered_outcome_uncertain": True}, quantity=9)
    tally, mapping = _CategoryTally(), {}
    await GitRestoreService()._restore_archives(
        db_session, {"archives": [entry(recovery_identity=a.recovery_identity, quantity=0)]}, True, tally, mapping
    )
    assert tally.skipped == 1 and tally.restored == 0 and mapping == {91: a.id}
    assert a.quantity == 9 and a.extra_data["recovered_outcome_uncertain"] is True


async def test_e11_normal_presence_matrix_immutable_ownership_and_zero(db_session):
    p = await printer(db_session)
    order = Project(name="Order")
    db_session.add(order)
    await db_session.commit()
    a = await archive(
        db_session,
        project_id=order.id,
        printer_id=p.id,
        filename="attached.3mf",
        file_size=900,
        content_hash="a" * 64,
        source_content_hash="b" * 64,
        file_path="/local",
        notes="keep",
        quantity=5,
        print_name="old",
        filament_used_grams=42,
        total_layers=10,
        cost=3,
    )
    await restore(
        db_session,
        [
            entry(
                recovery_identity=a.recovery_identity,
                filename="foreign",
                file_size=1,
                content_hash="c" * 64,
                source_content_hash="d" * 64,
                file_path=None,
                started_at=END.isoformat(),
                printer_id=None,
                project_id=None,
                project_line_id=17,
                quantity=0,
                is_favorite=False,
                print_name=None,
                filament_used_grams=0,
                total_layers=True,
                cost="42",
                notes="new",
            )
        ],
    )
    assert (a.filename, a.file_size, a.file_path, a.content_hash, a.source_content_hash, a.started_at) == (
        "attached.3mf",
        900,
        "/local",
        "a" * 64,
        "b" * 64,
        AT,
    )
    assert (a.project_id, a.printer_id) == (order.id, p.id)
    assert (a.quantity, a.is_favorite, a.print_name, a.filament_used_grams, a.total_layers, a.cost, a.notes) == (
        0,
        False,
        None,
        0,
        10,
        3,
        "new",
    )


async def test_e12_uncertain_history_survives_rebackup_without_authority(db_session):
    p = await printer(db_session)
    await restore(
        db_session,
        [entry(status="printing", recovery_identity=str(uuid4()), printer_id=p.id, printer_serial=p.serial_number)],
    )
    a = (await db_session.scalars(select(PrintArchive))).one()
    payload = await collect(db_session)
    row = payload[ARCHIVES_PATH]["archives"][0]
    assert row["recovered_outcome_uncertain"] is True
    await restore(db_session, [dict(row, recovered_outcome_uncertain=False, quantity=8, notes="operator")])
    assert a.extra_data["recovered_outcome_uncertain"] is True and a.quantity == 1 and a.notes == "operator"


@pytest.mark.parametrize(
    "bundle",
    [
        {},
        {"energy_kwh": 4},
        {"energy_kwh": None, "energy_cost": None, "energy_record": {}},
        {"energy_kwh": float("nan"), "energy_cost": 1, "energy_record": {}},
        {
            "energy_kwh": 1,
            "energy_cost": 1,
            "energy_record": {"energy_provenance": {"kind": "estimated", "coverage": 2}},
        },
        {
            "energy_kwh": 1,
            "energy_cost": 1,
            "energy_record": {"energy_provenance": {"kind": "measured", "plug_id": True}},
        },
    ],
)
async def test_e14_incomplete_no_energy_and_invalid_overwrite_keep_whole_bundle(db_session, bundle):
    original = {"energy_provenance": {"kind": "measured", "coverage": 1, "final": True}, "unrelated": "keep"}
    a = await archive(db_session, energy_kwh=2, energy_cost=3, extra_data=original.copy())
    await restore(db_session, [entry(recovery_identity=a.recovery_identity, **bundle)])
    assert (a.energy_kwh, a.energy_cost, a.extra_data) == (2, 3, original)


async def test_e14_zero_whole_write_explicit_clear_and_unrelated_json(db_session):
    a = await archive(db_session, energy_kwh=2, energy_cost=3, extra_data={"unrelated": "keep"})
    await restore(
        db_session, [entry(recovery_identity=a.recovery_identity, energy_kwh=0, energy_cost=0, energy_record={})]
    )
    assert (a.energy_kwh, a.energy_cost, a.extra_data) == (0, 0, {"unrelated": "keep"})
    await restore(
        db_session,
        [entry(recovery_identity=a.recovery_identity, energy_kwh=None, energy_cost=None, energy_record=None)],
    )
    assert (a.energy_kwh, a.energy_cost, a.extra_data) == (None, None, {"unrelated": "keep"})


@pytest.mark.parametrize(
    "bundle",
    [
        {"energy_kwh": True, "energy_cost": 1, "energy_record": {}},
        {
            "energy_kwh": 1,
            "energy_cost": 1,
            "energy_record": {"energy_provenance": {"kind": "unavailable", "unavailable_reason": "unknown"}},
        },
        {
            "energy_kwh": 1,
            "energy_cost": 1,
            "energy_record": {"energy_provenance": {"kind": "estimated", "coverage": float("nan")}},
        },
    ],
)
async def test_e13_invalid_insert_energy_is_unknown_not_a_false_measurement(db_session, bundle):
    await restore(db_session, [entry(**bundle)])
    a = (await db_session.scalars(select(PrintArchive))).one()
    assert (a.energy_kwh, a.energy_cost) == (None, None)
    assert meter_watch.energy_wire_fields(a.extra_data, a.energy_kwh)["energy_status"] == "unavailable"


async def test_e21_summary_counts_are_exact_bounded_and_do_not_echo_backup(db_session):
    rows = [entry(id=i + 1, filename=f"SECRET-{i}", recovery_identity="bad") for i in range(35)]
    rows.extend([False, entry(id=True)])
    result = (await restore(db_session, rows))["archives"]
    assert (result.restored, result.skipped, result.failed) == (0, 35, 2)
    assert params(result)["identity_skipped"] == 35 and params(result)["failed_rows"] == 2
    assert len(result.notes) <= 20 and "SECRET" not in json.dumps(result.notes)


async def test_e11_every_normal_scalar_group_roundtrips_without_importing_extra_data(db_session):
    a = await archive(db_session, extra_data={"local": "keep"})
    fields = {
        "print_name": "updated",
        "filament_type": "PETG",
        "filament_color": "FFFFFF",
        "sliced_for_model": "A1",
        "status": "failed",
        "failure_reason": "adhesionFailure",
        "makerworld_url": "https://example.com/model",
        "designer": "author",
        "external_url": "https://example.com/source",
        "notes": "operator",
        "tags": "tag",
        "is_favorite": True,
        "completed_at": END.isoformat(),
        "deleted_at": None,
        "print_time_seconds": 0,
        "total_layers": 693,
        "bed_temperature": 0,
        "nozzle_temperature": 240,
        "quantity": 0,
        "filament_used_grams": 0,
        "layer_height": 0.2,
        "nozzle_diameter": 0.4,
        "cost": 0,
    }
    await restore(
        db_session, [entry(recovery_identity=a.recovery_identity, extra_data={"foreign": "authority"}, **fields)]
    )
    for key, value in fields.items():
        assert getattr(a, key) == (END if key == "completed_at" else value), key
    assert a.extra_data == {"local": "keep"}
