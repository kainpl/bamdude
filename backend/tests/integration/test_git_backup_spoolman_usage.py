"""A Git backup carries the Spoolman usage history, and a restore binds it to the restoring side's Spoolman.

The rows travel in a file of their own (``spools/spoolman_usage_history.json``) in a portable form: no local
row, user or printer id, no source token — a printer by its serial, an archive by its id INSIDE the backup
(resolved through the archives the same restore brought), and one bit saying whether the row belongs to the
exporting side's current Spoolman. A restore writes through the one writer, under the restoring installation's
current source token, and never twice (upstream 1d86cff5a).
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import delete, select

from backend.app.models.archive import PrintArchive
from backend.app.models.printer import Printer
from backend.app.models.settings import Settings
from backend.app.models.spoolman_usage_history import SpoolmanUsageHistory
from backend.app.schemas.git_backup import RestoreCategory
from backend.app.services import spoolman_usage_history as history
from backend.app.services.git_backup import GitBackupService
from backend.app.services.git_restore import (
    ARCHIVES_PATH,
    SPOOLMAN_USAGE_PATH,
    SPOOLS_PATH,
    GitRestoreService,
    _CategoryTally,
)

pytestmark = pytest.mark.integration

SERIAL = "01P00A000000077"
REGISTERED = "2026-09-01T08:00:00Z"
# Everything a row says that is not a local id.
PORTABLE = (
    "spoolman_spool_id",
    "spool_registered",
    "print_name",
    "weight_used",
    "percent_used",
    "status",
    "cost",
    "created_at",
    "material",
    "subtype",
    "brand",
    "color_name",
    "rgba",
    "slicer_filament_name",
    "label_weight",
)


async def use_spoolman(db, url: str) -> str:
    row = (await db.execute(select(Settings).where(Settings.key == "spoolman_url"))).scalar_one_or_none()
    if row is None:
        db.add(Settings(key="spoolman_url", value=url))
    else:
        row.value = url
    await db.commit()
    return await history.source_token(db)


def a_printer(serial: str = SERIAL) -> Printer:
    return Printer(name="P1S", serial_number=serial, ip_address="192.168.1.77", access_code="12345678", model="P1S")


def an_archive(name: str = "benchy") -> PrintArchive:
    return PrintArchive(
        filename=f"{name}.gcode.3mf",
        file_path="",
        file_size=1,
        print_name=name,
        status="completed",
        started_at=datetime(2026, 10, 1, 12, 0, 0),
    )


def a_row(token: str, spool_id: int, **over) -> SpoolmanUsageHistory:
    fields = {
        "source_token": token,
        "spoolman_spool_id": spool_id,
        "spool_registered": REGISTERED,
        "print_name": "benchy",
        "weight_used": 12.5,
        "percent_used": 1,
        "status": "completed",
        "cost": 0.31,
        "created_at": datetime(2026, 10, 1, 13, 30, 5, 123456),
        "material": "PETG",
        "subtype": "HF",
        "brand": "Bambu Lab",
        "color_name": "Green",
        "rgba": "00FF00FF",
        "slicer_filament_name": "Bambu PETG HF",
        "label_weight": 1000.0,
    }
    return SpoolmanUsageHistory(**{**fields, **over})


async def seeded(db) -> str:
    """A printer, an archive and three rows of the current Spoolman; returns its token."""
    token = await use_spoolman(db, "http://spoolman.local:7912")
    printer, archive = a_printer(), an_archive()
    db.add_all([printer, archive])
    await db.commit()
    db.add_all(
        [
            a_row(token, 7, printer_id=printer.id, archive_id=archive.id),
            # A spool Spoolman did not hand over at charge time: no snapshot, no price.
            a_row(
                token,
                8,
                spool_registered=None,
                print_name=None,
                cost=None,
                status="failed",
                weight_used=3.25,
                created_at=datetime(2026, 10, 2, 9, 0, 0),
                **dict.fromkeys(
                    ("material", "subtype", "brand", "color_name", "rgba", "slicer_filament_name", "label_weight")
                ),
            ),
            a_row(token, 7, weight_used=40.0, percent_used=4, created_at=datetime(2026, 10, 3, 9, 0, 0)),
        ]
    )
    await db.commit()
    return token


async def backed_up(db) -> dict:
    files: dict = {}
    service = GitBackupService()
    await service._collect_spools(db, files)
    await service._collect_archives(db, files)
    await service._collect_settings(db, files)
    # What a restore reads is what the provider stored: JSON text.
    return json.loads(json.dumps(files))


async def restore(db, files: dict, categories=(RestoreCategory.ARCHIVES, RestoreCategory.SPOOLS)) -> _CategoryTally:
    results = await GitRestoreService()._apply(db, files, list(categories), False)
    await db.commit()
    return results[RestoreCategory.SPOOLS.value]


async def rows(db) -> list[SpoolmanUsageHistory]:
    result = await db.execute(
        select(SpoolmanUsageHistory).order_by(SpoolmanUsageHistory.id).execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


def portable(row: SpoolmanUsageHistory) -> tuple:
    return tuple(getattr(row, name) for name in PORTABLE)


async def test_the_backup_file_is_portable(db_session):
    token = await seeded(db_session)

    files = await backed_up(db_session)

    payload = files[SPOOLMAN_USAGE_PATH]
    assert payload["version"] == "1.0" and payload["entry_count"] == 3
    first = payload["usage_history"][0]
    assert first == {
        "spoolman_spool_id": 7,
        "spool_registered": REGISTERED,
        "current_source": True,
        "printer_serial": SERIAL,
        "archive_id": files[ARCHIVES_PATH]["archives"][0]["id"],
        "print_name": "benchy",
        "weight_used": 12.5,
        "percent_used": 1,
        "status": "completed",
        "cost": 0.31,
        "created_at": "2026-10-01T13:30:05.123456",
        "material": "PETG",
        "subtype": "HF",
        "brand": "Bambu Lab",
        "color_name": "Green",
        "rgba": "00FF00FF",
        "slicer_filament_name": "Bambu PETG HF",
        "label_weight": 1000.0,
    }
    # No local row / user / printer id and no source token — in this file or any other of the backup.
    for entry in payload["usage_history"]:
        assert not {"id", "source_token", "printer_id", "user_id", "created_by_id"} & set(entry)
    assert token not in json.dumps(files)
    assert history.SOURCE_SETTING not in files["settings/app_settings.json"]["settings"]


async def test_a_round_trip_brings_the_rows_back_under_the_local_printer_and_archive(db_session):
    await seeded(db_session)
    before = sorted(portable(r) for r in await rows(db_session))
    files = await backed_up(db_session)

    # The installation is rebuilt: other rows hold the ids the backup's printer and archive had.
    for model in (SpoolmanUsageHistory, PrintArchive, Printer):
        await db_session.execute(delete(model))
    await db_session.commit()
    db_session.add_all([a_printer("01P00A000000001"), an_archive("somebody-elses")])
    await db_session.commit()
    db_session.add(a_printer())
    await db_session.commit()

    tally = await restore(db_session, files)

    restored = await rows(db_session)
    assert sorted(portable(r) for r in restored) == before
    assert (tally.failed, tally.skipped) == (0, 0)
    printer = (await db_session.execute(select(Printer).where(Printer.serial_number == SERIAL))).scalar_one()
    archive = (await db_session.execute(select(PrintArchive).where(PrintArchive.print_name == "benchy"))).scalar_one()
    linked = next(r for r in restored if r.weight_used == 12.5)
    assert (linked.printer_id, linked.archive_id) == (printer.id, archive.id)
    assert all((r.printer_id, r.archive_id) == (None, None) for r in restored if r is not linked)
    assert {r.source_token for r in restored} == {await history.source_token(db_session)}


async def test_a_second_restore_duplicates_nothing(db_session):
    await seeded(db_session)
    files = await backed_up(db_session)
    await db_session.execute(delete(SpoolmanUsageHistory))
    await db_session.commit()

    first = await restore(db_session, files)
    second = await restore(db_session, files)

    assert len(await rows(db_session)) == 3
    # The spool inventory file is empty here, so the tallies are the usage rows alone.
    assert (first.restored, first.skipped) == (3, 0)
    assert (second.restored, second.skipped, second.failed) == (0, 3, 0)


async def test_rows_already_there_are_recognised_by_what_they_say(db_session):
    """The live rows were never deleted: a restore over them adds nothing."""
    await seeded(db_session)
    files = await backed_up(db_session)

    tally = await restore(db_session, files)

    assert len(await rows(db_session)) == 3 and (tally.restored, tally.skipped) == (0, 3)


async def test_two_identical_charges_stay_two(db_session):
    token = await use_spoolman(db_session, "http://spoolman.local:7912")
    db_session.add_all([a_row(token, 7), a_row(token, 7)])
    await db_session.commit()
    files = await backed_up(db_session)
    await db_session.execute(delete(SpoolmanUsageHistory))
    await db_session.commit()

    await restore(db_session, files)
    await restore(db_session, files)

    assert len(await rows(db_session)) == 2


async def test_the_rows_are_bound_to_the_restoring_sides_current_spoolman(db_session):
    exported_under = await seeded(db_session)
    files = await backed_up(db_session)
    await db_session.execute(delete(SpoolmanUsageHistory))
    await db_session.commit()
    # The restoring installation talks to another address: its own source.
    current = await use_spoolman(db_session, "http://other-host:7912")
    assert current != exported_under

    await restore(db_session, files)

    assert {r.source_token for r in await rows(db_session)} == {current}


async def test_a_previous_spoolmans_rows_are_marked_and_not_attached_to_the_current_one(db_session):
    old = await use_spoolman(db_session, "http://old-host:7912")
    db_session.add(a_row(old, 7, weight_used=99.0))
    await db_session.commit()
    current = await use_spoolman(db_session, "http://spoolman.local:7912")
    db_session.add(a_row(current, 7))
    await db_session.commit()

    files = await backed_up(db_session)

    flags = {e["weight_used"]: e["current_source"] for e in files[SPOOLMAN_USAGE_PATH]["usage_history"]}
    assert flags == {99.0: False, 12.5: True}

    await db_session.execute(delete(SpoolmanUsageHistory))
    await db_session.commit()
    tally = await restore(db_session, files)

    assert [r.weight_used for r in await rows(db_session)] == [12.5]
    assert (tally.restored, tally.skipped, tally.failed) == (1, 1, 0)
    assert [n["code"] for n in tally.notes] == ["spoolmanUsageOtherSource"]


async def test_a_malformed_entry_is_counted_and_never_stops_the_restore(db_session):
    await use_spoolman(db_session, "http://spoolman.local:7912")
    good = {"spoolman_spool_id": 7, "current_source": True, "weight_used": 5, "created_at": "2026-10-01T13:30:05"}
    entries = [
        "not an entry",
        {**good, "spoolman_spool_id": None},
        {**good, "spoolman_spool_id": True},
        {**good, "spoolman_spool_id": 0},
        {**good, "created_at": "yesterday"},
        {**good, "created_at": None},
        {**good, "weight_used": "a lot"},
        {**good, "weight_used": -1},
        {**good, "weight_used": float("nan")},
        {**good, "cost": "free", "status": 7, "percent_used": "x", "material": ["PETG"], "rgba": "00FF00FF00FF"},
        good,
    ]
    tally = _CategoryTally()

    # As the SPOOLS category runs it: under the Spoolman source guard (C047 S29, S30).
    from backend.app.services import spoolman_source

    async with spoolman_source.guard():
        await GitRestoreService()._restore_spoolman_usage(db_session, {"usage_history": entries}, tally, {})
        await db_session.commit()

    assert (tally.restored, tally.failed, tally.skipped) == (2, 9, 0)
    lenient = next(r for r in await rows(db_session) if r.weight_used == 5 and r.rgba is not None)
    # Unusable optional fields are dropped, text is cut to its column; the charge itself is history.
    assert (lenient.cost, lenient.status, lenient.percent_used, lenient.material, lenient.rgba) == (
        None,
        "completed",
        0,
        None,
        "00FF00FF",
    )


async def test_a_payload_that_is_not_a_list_restores_nothing(db_session):
    await use_spoolman(db_session, "http://spoolman.local:7912")
    tally = _CategoryTally()

    for payload in (None, [], {"usage_history": "rows"}, {"usage_history": []}):
        await GitRestoreService()._restore_spoolman_usage(db_session, payload, tally, {})

    assert await rows(db_session) == [] and (tally.restored, tally.skipped, tally.failed) == (0, 0, 0)


async def test_an_installation_that_never_named_a_spoolman_restores_none_and_says_so(db_session):
    await seeded(db_session)
    files = await backed_up(db_session)
    for model in (SpoolmanUsageHistory, Settings):
        await db_session.execute(delete(model))
    await db_session.commit()

    tally = await restore(db_session, files)

    assert await rows(db_session) == []
    assert (tally.restored, tally.skipped) == (0, 3)
    assert "spoolmanUsageNoSource" in [n["code"] for n in tally.notes]


async def test_the_rows_come_back_while_the_built_in_inventory_is_in_use(db_session):
    """As the built-in file, which is restored in Spoolman mode too: the mode decides what is shown, not kept."""
    await seeded(db_session)
    files = await backed_up(db_session)
    await db_session.execute(delete(SpoolmanUsageHistory))
    db_session.add(Settings(key="spoolman_enabled", value="false"))
    await db_session.commit()

    await restore(db_session, files)

    assert len(await rows(db_session)) == 3


async def test_spools_without_archives_restores_the_rows_without_their_print_link(db_session):
    await seeded(db_session)
    files = await backed_up(db_session)
    await db_session.execute(delete(SpoolmanUsageHistory))
    await db_session.commit()

    tally = await restore(db_session, files, categories=(RestoreCategory.SPOOLS,))

    assert [r.archive_id for r in await rows(db_session)] == [None, None, None]
    assert {"code": "spoolUsageUnlinked", "params": {"count": 1}} in [
        {"code": n["code"], "params": n["params"]} for n in tally.notes
    ]


async def test_the_preview_counts_the_spoolman_rows_and_the_category_fetches_the_file(db_session):
    await seeded(db_session)
    files = await backed_up(db_session)
    service = GitRestoreService()

    assert SPOOLMAN_USAGE_PATH in service._category_paths(RestoreCategory.SPOOLS, list(files))
    count, detail = await service._count_items(db_session, RestoreCategory.SPOOLS, files)

    assert count == len(files[SPOOLS_PATH]["spools"]) + 3
    assert (detail.code, detail.params) == ("spoolsUsageCount", {"count": 3})


async def test_the_export_writes_nothing(db_session):
    """An address changed but not yet used: the export does not rotate the source on the way."""
    await seeded(db_session)
    setting = (await db_session.execute(select(Settings).where(Settings.key == "spoolman_url"))).scalar_one()
    setting.value = "http://moved:7912"
    await db_session.commit()
    stored = (
        await db_session.execute(select(Settings.value).where(Settings.key == history.SOURCE_SETTING))
    ).scalar_one()

    files = await backed_up(db_session)

    again = (
        await db_session.execute(select(Settings.value).where(Settings.key == history.SOURCE_SETTING))
    ).scalar_one()
    assert again == stored
    # The rows are the address's that was left: not the current Spoolman's.
    assert [e["current_source"] for e in files[SPOOLMAN_USAGE_PATH]["usage_history"]] == [False, False, False]


async def test_no_cloud_sign_in_token_of_the_installation_is_exported(db_session):
    """The ownerless Bambu / Orca Cloud sign-in lives in ``Settings`` rows: the
    access token, the refresh token (which mints access tokens) and a pending
    device code never reach ``settings/app_settings.json``."""
    from backend.app.models.settings import Settings

    secrets = {
        "bambu_cloud_token": "fabricated-bambu-access",
        "bambu_cloud_refresh_token": "fabricated-bambu-refresh",
        "orca_cloud_token": "fabricated-orca-access",
        "orca_cloud_refresh_token": "fabricated-orca-refresh",
        "orca_cloud_pending_verifier": "fabricated-device-code",
    }
    db_session.add_all([Settings(key=key, value=value) for key, value in secrets.items()])
    db_session.add(Settings(key="bambu_cloud_region", value="global"))
    await db_session.commit()
    files: dict = {}

    await GitBackupService()._collect_settings(db_session, files)

    exported = files["settings/app_settings.json"]["settings"]
    assert not set(secrets) & set(exported)
    assert not [value for value in secrets.values() if value in json.dumps(files)]
    assert exported["bambu_cloud_region"] == "global"


async def test_reused_archive_id_between_categories_does_not_capture_spoolman_usage(db_session):
    await seeded(db_session)
    files = await backed_up(db_session)
    old_id = await db_session.scalar(select(PrintArchive.id))
    await db_session.execute(delete(SpoolmanUsageHistory))
    await db_session.commit()

    class ReusingRestore(GitRestoreService):
        async def _restore_spools(self, db, *args, **kwargs):
            await db.execute(delete(PrintArchive).where(PrintArchive.id == old_id))
            db.add(PrintArchive(id=old_id, filename="other-print", file_path="", file_size=0, status="completed"))
            await db.commit()
            await self._begin_record_category(db)
            return await super()._restore_spools(db, *args, **kwargs)

    result = await ReusingRestore()._apply(db_session, files, [RestoreCategory.ARCHIVES, RestoreCategory.SPOOLS], False)
    restored = await rows(db_session)
    assert len(restored) == 3
    assert all(row.archive_id is None for row in restored)
    assert any(note["code"] == "spoolUsageUnlinked" for note in result["spools"].notes)
