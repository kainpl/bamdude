"""Observe S1 spool DB state, collector output, mappings and bounded notes."""

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.schemas.git_backup import RestoreCategory
from backend.app.services.git_backup import GitBackupService
from backend.app.services.git_restore import SPOOL_USAGE_PATH, SPOOLS_PATH, GitRestoreService, _CategoryTally

pytestmark = pytest.mark.asyncio


async def make_spool(db, **changes):
    row = Spool(
        **{
            "material": "PLA",
            "brand": "Maker",
            "tag_uid": "TAG-A",
            "core_weight": 310,
            "weight_used": 200,
            "weight_used_baseline": 150,
            "weight_locked": True,
            "last_used": datetime(2026, 10, 2, 13),
            "created_at": datetime(2026, 10, 1, 12),
            **changes,
        }
    )
    db.add(row)
    await db.commit()
    return row


async def restore(db, entries, *, overwrite=True, tally=None, service=None):
    tally = tally or _CategoryTally()
    await (service or GitRestoreService())._restore_spools(db, {"spools": entries}, None, overwrite, tally, {})
    await db.commit()
    return tally


def summary(tally):
    notes = [n["params"] for n in tally.notes if n["code"] == "spoolInputSummary"]
    assert len(notes) == 1
    return notes[0]


async def test_actual_collector_roundtrip_and_exact_reactivation_count(db_session):
    row = await make_spool(db_session, data_origin="manual", tag_type="bambulab")
    await make_spool(db_session, tag_uid="ARCHIVED", archived_at=datetime(2026, 10, 2))
    db_session.add(SpoolUsageHistory(spool_id=row.id, weight_used=10, status="completed"))
    await db_session.commit()
    files = {}
    await GitBackupService()._collect_spools(db_session, files)
    files = json.loads(json.dumps(files))  # The actual JSON writer's scalar types.
    entries = files[SPOOLS_PATH]["spools"]
    assert len(entries) == 1 and len(files[SPOOL_USAGE_PATH]["history"]) == 1
    assert {k: entries[0][k] for k in ("core_weight", "weight_locked", "weight_used_baseline", "archived_at")} == {
        "core_weight": 310,
        "weight_locked": True,
        "weight_used_baseline": 150,
        "archived_at": None,
    }
    row.core_weight, row.weight_locked, row.weight_used_baseline = 999, False, 199
    row.archived_at, row.last_used = datetime(2026, 10, 3), None
    row.data_origin, row.tag_type = None, None
    await db_session.commit()
    tally = await restore(db_session, entries)
    await db_session.refresh(row)
    assert (row.core_weight, row.weight_locked, row.weight_used_baseline, row.last_used, row.archived_at) == (
        310,
        True,
        150,
        datetime(2026, 10, 2, 13),
        None,
    )
    assert (row.data_origin, row.tag_type) == ("manual", "bambulab")
    assert summary(tally)["reactivated"] == 1
    assert await db_session.scalar(select(func.count()).select_from(SpoolUsageHistory)) == 1
    again = await restore(db_session, entries)
    assert not any(n["code"] == "spoolInputSummary" for n in again.notes)


async def test_older_backup_keeps_absent_local_metadata_and_tags(db_session):
    row = await make_spool(db_session, archived_at=datetime(2026, 10, 3), tray_uuid="NEW-SCAN")
    tally = await restore(db_session, [{"id": 41, "tag_uid": "TAG-A"}])
    await db_session.refresh(row)
    assert (
        row.core_weight,
        row.weight_locked,
        row.weight_used_baseline,
        row.archived_at,
        row.last_used,
        row.tray_uuid,
    ) == (
        310,
        True,
        150,
        datetime(2026, 10, 3),
        datetime(2026, 10, 2, 13),
        "NEW-SCAN",
    )
    assert tally.restored == 1 and not tally.notes


@pytest.mark.parametrize(
    "key,bad,expected",
    [
        ("core_weight", True, 310),
        ("label_weight", 1.5, 1000),
        ("weight_used", "12.5", 200),
        ("weight_used_baseline", None, 150),
        ("weight_locked", "false", True),
        ("brand", [], "Maker"),
        ("tag_uid", {}, "TAG-A"),
        ("last_used", "0001-01-01T00:00:00+14:00", datetime(2026, 10, 2, 13)),
    ],
)
async def test_invalid_overwrite_keeps_local_and_reports_field(db_session, key, bad, expected):
    row = await make_spool(db_session)
    entry = {"id": 41, "tag_uid": "TAG-A", "tray_uuid": "TRAY-A", key: bad}
    row.tray_uuid = "TRAY-A"
    await db_session.commit()
    tally = await restore(db_session, [entry])
    await db_session.refresh(row)
    assert getattr(row, key) == expected
    assert summary(tally)["invalid_fields"] == 1


async def test_invalid_insert_uses_reviewed_defaults_and_generated_identity(db_session):
    tally = await restore(
        db_session,
        [
            {
                "id": 999,
                "tag_uid": "NEW",
                "material": [],
                "label_weight": False,
                "core_weight": None,
                "weight_used": "10",
                "weight_used_baseline": None,
                "weight_locked": "false",
                "cost_per_kg": float("inf"),
                "last_used": "bad",
            }
        ],
    )
    row = (await db_session.execute(select(Spool))).scalar_one()
    assert row.id != 999
    assert (row.material, row.label_weight, row.core_weight, row.weight_used, row.weight_used_baseline) == (
        "PLA",
        1000,
        250,
        0,
        0,
    )
    assert row.weight_locked is False and row.cost_per_kg is None and row.last_used is None
    assert tally.restored == 1 and summary(tally)["invalid_fields"] == 8


@pytest.mark.parametrize("insert", [True, False])
async def test_explicit_zero_false_and_above_used_anchor_are_preserved(db_session, insert):
    if not insert:
        await make_spool(db_session)
    tally = await restore(
        db_session,
        [
            {
                "id": 41,
                "tag_uid": "TAG-A",
                "label_weight": 0,
                "core_weight": 0,
                "weight_used": 0,
                "weight_used_baseline": 150,
                "weight_locked": False,
            }
        ],
    )
    row = (await db_session.execute(select(Spool))).scalar_one()
    assert (row.label_weight, row.core_weight, row.weight_used, row.weight_used_baseline, row.weight_locked) == (
        0,
        0,
        0,
        150,
        False,
    )
    assert summary(tally)["baseline_above_used"] == 1
    assert max(0, row.weight_used - row.weight_used_baseline) == 0
    assert row.label_weight - row.weight_used == 0  # Baseline never changes remaining.


@pytest.mark.parametrize("stamp,expected", [(None, None), ("2026-10-04T14:00:00+02:00", datetime(2026, 10, 4, 12))])
async def test_last_used_clear_and_utc_overwrite(db_session, stamp, expected):
    row = await make_spool(db_session)
    await restore(db_session, [{"tag_uid": "TAG-A", "last_used": stamp}])
    await db_session.refresh(row)
    assert row.last_used == expected


async def test_ambiguous_tag_and_missing_identity_have_no_writes_or_maps(db_session, monkeypatch):
    await make_spool(db_session)
    await make_spool(db_session)
    service = GitRestoreService()
    usage = AsyncMock()
    monkeypatch.setattr(service, "_restore_spool_usage", usage)
    tally = await restore(db_session, [{"id": 41, "tag_uid": "TAG-A", "core_weight": 1}, {"id": 42}], service=service)
    assert tally.skipped == 2 and usage.call_args.args[3] == {}
    assert summary(tally)["skipped_rows"] == 2
    assert (await db_session.execute(select(Spool.core_weight))).scalars().all() == [310, 310]


async def test_duplicate_source_ids_and_bool_are_failed_before_mutation(db_session, monkeypatch):
    service = GitRestoreService()
    usage = AsyncMock()
    monkeypatch.setattr(service, "_restore_spool_usage", usage)
    tally = await restore(
        db_session,
        [
            {"id": True, "tag_uid": "BOOL"},
            {"id": 41, "tag_uid": "ONE"},
            {"id": 41, "tag_uid": "TWO"},
            {"id": 42, "tag_uid": "GOOD"},
        ],
        service=service,
    )
    rows = (await db_session.execute(select(Spool))).scalars().all()
    assert tally.failed == 3 and tally.restored == 1
    assert len(rows) == 1 and rows[0].tag_uid == "GOOD"
    assert usage.call_args.args[3] == {42: rows[0].id}


async def test_composite_match_is_unique_and_repeated_restore_is_idempotent(db_session):
    entry = {"id": 41, "material": "PLA", "created_at": "2026-10-02T14:00:00+02:00", "last_used": None}
    first = await restore(db_session, [entry])
    second = await restore(db_session, [entry], overwrite=False)
    assert (first.restored, second.skipped) == (1, 1)
    row = (await db_session.execute(select(Spool))).scalar_one()
    assert row.created_at == datetime(2026, 10, 2, 12)
    db_session.add(Spool(material="PLA", created_at=row.created_at))
    await db_session.commit()
    third = await restore(db_session, [{**entry, "core_weight": 1}])
    assert third.skipped == 1
    assert (await db_session.execute(select(Spool.core_weight))).scalars().all() == [250, 250]


async def test_large_invalid_payload_reports_exact_counts_with_twenty_note_cap(db_session):
    tally = _CategoryTally()
    for index in range(20):
        tally.note("existing", "existing caveat", count=index)
    entries = [{"id": i + 1, "tag_uid": f"T{i}", "weight_used": "bad", "weight_locked": "false"} for i in range(100)]
    await restore(db_session, entries, tally=tally)
    assert tally.restored == 100 and len(tally.notes) == 20
    assert summary(tally)["invalid_fields"] == 200
    assert summary(tally)["failed_rows"] == 0
    assert "T99" not in json.dumps(tally.notes)


async def test_s1_still_ignores_canonical_usage_alias(db_session):
    row = await make_spool(db_session)
    service = GitRestoreService()
    tally = _CategoryTally()
    await service._restore_spools(
        db_session,
        {"spools": [{"id": 41, "tag_uid": "TAG-A"}]},
        {
            "history": [{"spool_id": 41, "weight_used": 10, "created_at": "2026-10-02T12:00:00"}],
        },
        True,
        tally,
        {},
    )
    await db_session.commit()
    assert tally.restored == 1 and row.weight_used == 200
    assert await db_session.scalar(select(func.count()).select_from(SpoolUsageHistory)) == 0


@pytest.mark.parametrize("overwrite", [False, True])
async def test_bulk_reactivation_count_only_includes_changed_spools(db_session, overwrite):
    rows = [
        await make_spool(db_session, tag_uid=f"T{i}", archived_at=datetime(2026, 10, 3) if i < 2 else None)
        for i in range(3)
    ]
    tally = await restore(db_session, [{"tag_uid": r.tag_uid, "archived_at": None} for r in rows], overwrite=overwrite)
    for row in rows:
        await db_session.refresh(row)
    if overwrite:
        assert all(row.archived_at is None for row in rows)
        assert summary(tally)["reactivated"] == 2
    else:
        assert sum(row.archived_at is not None for row in rows) == 2
        assert tally.skipped == 3 and not tally.notes


@pytest.mark.parametrize(
    "entry", [{"created_at": "0001-01-01T00:00:00+14:00"}, {"created_at": "2026-10-01T12:00:00", "brand": []}]
)
async def test_invalid_composite_cannot_become_an_insert_identity(db_session, entry):
    tally = await restore(db_session, [entry])
    assert tally.skipped == 1 and tally.restored == 0
    assert await db_session.scalar(select(func.count()).select_from(Spool)) == 0


@pytest.mark.parametrize(
    "failure", [IntegrityError("synthetic flush", {}, Exception("bad row")), asyncio.CancelledError()]
)
async def test_category_failure_cannot_publish_tentative_spools_or_maps(db_session, monkeypatch, failure):
    from backend.app.models.archive import PrintArchive

    real_flush = db_session.flush
    spool_flushes = 0

    async def fail_second_spool(*args, **kwargs):
        nonlocal spool_flushes
        if any(isinstance(row, Spool) for row in db_session.new):
            spool_flushes += 1
            if spool_flushes == 2:
                raise failure
        return await real_flush(*args, **kwargs)

    monkeypatch.setattr(db_session, "flush", fail_second_spool)
    service = GitRestoreService()
    usage = AsyncMock()
    monkeypatch.setattr(service, "_restore_spool_usage", usage)
    results = {}
    payload = {
        "archives/print_archives.json": {"archives": [{"id": 71, "filename": "previous-category", "file_size": 0}]},
        SPOOLS_PATH: {"spools": [{"id": 41, "tag_uid": "FIRST"}, {"id": 42, "tag_uid": "SECOND"}]},
    }
    with pytest.raises(type(failure)):
        await service._apply(
            db_session, payload, [RestoreCategory.ARCHIVES, RestoreCategory.SPOOLS], True, results=results
        )
    await db_session.rollback()  # Same category owner action as run_restore.
    assert set(results) == {"archives"} and results["archives"].restored == 1
    assert await db_session.scalar(select(func.count()).select_from(PrintArchive)) == 1
    assert await db_session.scalar(select(func.count()).select_from(Spool)) == 0
    usage.assert_not_awaited()


@pytest.mark.parametrize("column", ["tag_uid", "tray_uuid"])
@pytest.mark.parametrize("overwrite", [False, True])
async def test_p3_duplicate_backup_tags_skip_all_rows_before_insert(db_session, monkeypatch, column, overwrite):
    service = GitRestoreService()
    usage = AsyncMock()
    monkeypatch.setattr(service, "_restore_spool_usage", usage)
    other = "tray_uuid" if column == "tag_uid" else "tag_uid"
    entries = [
        {"id": 41, column: "SHARED", other: "A", "core_weight": "bad"},
        {"id": 42, column: "SHARED", other: "B"},
        {"id": 43, "tag_uid": "GOOD"},
    ]
    tally = await restore(db_session, entries, service=service, overwrite=overwrite)
    rows = (await db_session.execute(select(Spool))).scalars().all()
    assert len(rows) == 1 and rows[0].tag_uid == "GOOD"
    assert (tally.restored, tally.skipped, tally.failed) == (1, 2, 0)
    assert usage.call_args.args[3] == {43: rows[0].id}
    assert summary(tally)["invalid_fields"] == 0
    assert next(n["params"]["count"] for n in tally.notes if n["code"] == "spoolIdentityUnavailable") == 2


@pytest.mark.parametrize("column", ["tag_uid", "tray_uuid"])
async def test_p3_duplicate_backup_tags_do_not_overwrite_local_spool(db_session, column):
    row = await make_spool(db_session, **{column: "SHARED"})
    tally = await restore(
        db_session,
        [
            {"id": 41, column: "SHARED", "core_weight": 1},
            {"id": 42, column: "SHARED", "core_weight": 2},
        ],
    )
    await db_session.refresh(row)
    assert row.core_weight == 310
    assert (tally.restored, tally.skipped) == (0, 2)


@pytest.mark.parametrize("overwrite", [False, True])
@pytest.mark.parametrize("existing", [False, True])
async def test_p3_one_local_spool_cannot_receive_two_source_identities(db_session, monkeypatch, existing, overwrite):
    if existing:
        await make_spool(db_session, tray_uuid="TRAY-A")
        entries = [{"id": 41, "tag_uid": "TAG-A"}, {"id": 42, "tray_uuid": "TRAY-A"}]
    else:
        entries = [
            {"id": source, "tag_uid": tag, "material": "PLA", "created_at": "2026-10-01T12:00:00"}
            for source, tag in ((41, "A"), (42, "B"))
        ]
    service = GitRestoreService()
    usage = AsyncMock()
    monkeypatch.setattr(service, "_restore_spool_usage", usage)
    tally = await restore(db_session, entries, overwrite=overwrite, service=service)
    rows = (await db_session.execute(select(Spool))).scalars().all()
    assert len(rows) == 1
    assert rows[0].tag_uid == ("TAG-A" if existing else "A")
    assert usage.call_args.args[3] == {41: rows[0].id}
    assert tally.restored == (0 if existing and not overwrite else 1)
    assert tally.skipped == (2 if existing and not overwrite else 1)
    assert next(n["params"]["count"] for n in tally.notes if n["code"] == "spoolIdentityUnavailable") == 1


async def test_p3_invalid_fields_on_overwrite_off_rows_do_not_claim_corrections(db_session):
    row = await make_spool(db_session)
    tally = await restore(db_session, [{"id": 41, "tag_uid": "TAG-A", "weight_used": "bad"}], overwrite=False)
    await db_session.refresh(row)
    assert row.weight_used == 200
    assert (tally.restored, tally.skipped) == (0, 1)
    assert not tally.notes


@pytest.mark.parametrize("ambiguous", [False, True])
async def test_p3_invalid_fields_on_unproven_rows_are_not_counted(db_session, ambiguous):
    if ambiguous:
        await make_spool(db_session)
        await make_spool(db_session)
        entry = {"id": 41, "tag_uid": "TAG-A", "weight_used": "bad"}
    else:
        entry = {"id": 41, "created_at": "bad", "weight_used": "bad"}
    tally = await restore(db_session, [entry])
    assert (tally.restored, tally.skipped) == (0, 1)
    assert summary(tally)["invalid_fields"] == 0


async def test_p3_invalid_field_counts_include_only_successfully_restored_rows(db_session):
    await make_spool(db_session)
    tally = await restore(
        db_session,
        [
            {"id": 41, "tag_uid": "TAG-A", "weight_used": "bad"},
            {"id": 42, "created_at": "bad", "weight_used": "bad", "brand": []},
        ],
    )
    assert (tally.restored, tally.skipped) == (1, 1)
    assert summary(tally)["invalid_fields"] == 1
