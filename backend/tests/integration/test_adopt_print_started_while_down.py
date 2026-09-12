"""A print observed RUNNING on a fresh process with no live archive is adopted
(spec 2026-09-12): row, queue claim, energy, usage session, download — silently.

BamDude is off; somebody starts a print from the printer's screen; BamDude
comes up. The #1304 guard deliberately suppresses ``on_print_start`` for that
first RUNNING push, so before this change the print ran to the end and left no
trace at all. ``on_print_running_observed`` now adopts it through
``_adopt_running_print``.

⚠️ **Silently.** A "print started" ninety minutes late is noise, and the plate
check / smart-plug action / macros are answers to a start that just happened.
The spies below are the enforcement: adding a side effect to the adoption
without deciding about it here is a red test, not a style choice.

⚠️ **The hash twin is asked BEFORE the attach.**
``_discard_provisional_archive`` may only delete a row with nothing on disk, so
hashing first is what makes the discard safe — and a print that already had a
row under a name the lookup could not see must not end up with two.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.services.archive import ArchiveService

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

# ``remaining_time`` is SECONDS here — bambu_mqtt builds it as minutes × 60 —
# and the row records it verbatim for the §3.3 reconstruction.
RUNNING = {
    "filename": "/data/Metadata/plate_1.gcode",
    "subtask_name": "Кронштейн",
    "remaining_time": 1800,
    "raw_data": {},
    "ams_mapping": None,
}

#: Every side effect the spec forbids the adoption, as (patch target, attribute).
SILENT = (
    "notification",
    "macros",
    "plate_check",
    "plug_on_print_start",
    "missing_spools",
    "ws_print_start",
)


@pytest.fixture(autouse=True)
def _clean_module_state():
    """``_active_prints`` and ``_timelapse_baselines`` are module dicts. A key
    left by one test makes the next one return early and assert against
    nothing."""
    from backend.app.main import _active_prints, _timelapse_baselines

    _active_prints.clear()
    _timelapse_baselines.clear()
    yield
    _active_prints.clear()
    _timelapse_baselines.clear()


@pytest.fixture
def main_db(monkeypatch, test_engine, tmp_path):
    """The handler and every helper it reaches open their own sessions off the
    module-level factories; without both of these they would write to the
    developer's real database."""
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    (tmp_path / "archive").mkdir(parents=True, exist_ok=True)
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("backend.app.main.async_session", factory)
    monkeypatch.setattr("backend.app.core.database.async_session", factory)
    return factory


class _Spawned:
    """Capture what ``spawn_background_task`` was handed, without running it.

    The coroutine's frame still holds its arguments while it has not started,
    which is the only way to see *which archive* the download was scheduled
    for. Closed immediately so pytest does not report it as never awaited.
    """

    def __init__(self) -> None:
        self.names: list[str | None] = []
        self.locals: list[dict] = []

    def __call__(self, coro, *, name=None):
        self.names.append(name)
        frame = getattr(coro, "cr_frame", None)
        self.locals.append(dict(frame.f_locals) if frame is not None else {})
        coro.close()
        return MagicMock()


async def _drive_hook(
    *,
    db_session,
    printer_factory,
    live_archive=(None, None),
    extra_patches=(),
    data=None,
    pre_seed=None,
):
    """Run ``on_print_running_observed`` against the real test database with
    every outbound effect either spied on or stubbed."""
    from contextlib import ExitStack

    from backend.app.main import on_print_running_observed

    printer = await printer_factory()
    printer.auto_archive = True
    await db_session.commit()
    # ⚠️ An int, not the ORM object: ``expire_all()`` below expires every
    # attribute including the PK, and reading one back in a sync assertion is a
    # lazy load with no greenlet to run it in.
    printer_id = printer.id
    if pre_seed is not None:
        pre_seed(printer_id)

    pm = MagicMock()
    pm.get_status.return_value = None
    pm.get_client.return_value = None
    pm.is_awaiting_plate_clear.return_value = False

    ws = MagicMock()
    for attr in ("send_archive_created", "send_archive_updated", "send_print_start", "broadcast"):
        setattr(ws, attr, AsyncMock())

    spies = {
        "queue_claim": AsyncMock(),
        "energy_start": AsyncMock(return_value=True),
        "usage_session": AsyncMock(),
        "baseline": AsyncMock(),
        "notification": AsyncMock(),
        "macros": AsyncMock(),
        "plate_check": AsyncMock(),
        "plug_on_print_start": AsyncMock(),
        "missing_spools": AsyncMock(),
    }
    spies["ws_print_start"] = ws.send_print_start
    spawned = _Spawned()

    patches = [
        patch("backend.app.main.printer_manager", pm),
        patch("backend.app.main.ws_manager", ws),
        patch("backend.app.main.mqtt_relay", MagicMock(on_archive_created=AsyncMock())),
        patch("backend.app.main.smart_plug_manager", MagicMock(on_print_start=spies["plug_on_print_start"])),
        patch("backend.app.main.spawn_background_task", spawned),
        patch("backend.app.main._live_archive_for_running_print", AsyncMock(return_value=live_archive)),
        patch("backend.app.main.mark_queue_printing_for_printer", spies["queue_claim"]),
        patch("backend.app.main._record_energy_start", spies["energy_start"]),
        patch("backend.app.main._capture_timelapse_baseline_at_start", spies["baseline"]),
        patch("backend.app.main._send_print_start_notification", spies["notification"]),
        patch("backend.app.main.notify_missing_spool_assignments_on_print_start", spies["missing_spools"]),
        patch("backend.app.services.usage_tracker.on_print_start", spies["usage_session"]),
        patch("backend.app.services.macro_trigger.fire_event_macros", spies["macros"]),
        patch("backend.app.services.plate_detection.check_plate_empty", spies["plate_check"]),
        *extra_patches,
    ]

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await on_print_running_observed(printer_id, dict(RUNNING if data is None else data))

    db_session.expire_all()
    rows = (await db_session.execute(select(PrintArchive).order_by(PrintArchive.id))).scalars().all()
    return printer_id, rows, spies, spawned


class TestTheHookAdoptsThePrintItFound:
    async def test_no_live_archive_creates_the_provisional_row_and_stays_silent(
        self, db_session, printer_factory, main_db
    ):
        from backend.app.main import _active_prints

        printer_id, rows, spies, spawned = await _drive_hook(db_session=db_session, printer_factory=printer_factory)

        assert len(rows) == 1, f"one running print, {len(rows)} archive rows"
        row = rows[0]
        assert row.printer_id == printer_id
        assert row.status == "printing"
        assert row.file_path == "", "empty file_path is the marker the four retry triggers select on"
        assert row.started_at is None, (
            "the start is UNKNOWN, not now — attach_3mf_to_archive reconstructs it from the slicer estimate"
        )
        assert row.print_name == "Кронштейн"
        assert row.filename == "/data/Metadata/plate_1.gcode"
        assert row.plate_index == 1, "the live plate the hook saw"
        assert row.queue_id == printer_id, "the printer's own queue row (PrinterQueue.id == printer_id)"

        extra = row.extra_data or {}
        assert extra.get("recovered_start") == {
            "observed_at": extra.get("recovered_start", {}).get("observed_at"),
            "remaining_seconds": 1800,
        }, "remaining_time is SECONDS and is recorded verbatim"
        observed_at = datetime.fromisoformat(extra["recovered_start"]["observed_at"])
        assert observed_at.tzinfo is not None, "the reconstruction reads this back as an instant, not a local time"
        assert abs((datetime.now(timezone.utc) - observed_at).total_seconds()) < 120
        assert extra.get("energy_is_approximate") is True
        assert extra.get("energy_start_partial") is True, "the counter was read mid-print; the figure covers the rest"
        assert extra.get("original_subtask") == "Кронштейн"
        assert extra.get("_print_data", {}).get("subtask_name") == "Кронштейн", (
            "the download retry service reads its subtask/filename back out of here"
        )
        assert extra.get("no_3mf_available") is None, "nothing has been tried yet — the banner must stay dark"

        assert _active_prints.get((printer_id, "Кронштейн")) == row.id
        assert _active_prints.get((printer_id, "Кронштейн.3mf")) == row.id
        assert _active_prints.get((printer_id, "/data/Metadata/plate_1.gcode")) == row.id

        spies["queue_claim"].assert_awaited_once()
        assert spies["queue_claim"].await_args.kwargs["archive_id"] == row.id
        assert spies["queue_claim"].await_args.kwargs["options"]["plate_id"] == 1

        spies["energy_start"].assert_awaited_once()
        assert spies["energy_start"].await_args.kwargs["context"] == "recovered-start"
        assert spies["energy_start"].await_args.args[0].id == row.id

        # Without the session ``on_ams_change`` keeps running the remain%-based
        # weight sync until completion and the final write-off double-counts.
        spies["usage_session"].assert_awaited_once()

        assert spawned.names == ["adopt-running-print-download"], f"download task not scheduled: {spawned.names}"
        assert spawned.locals[0].get("archive_id") == row.id
        assert spawned.locals[0].get("printer_id") == printer_id

        for name in SILENT:
            assert not spies[name].called, f"{name} fired for an adoption — spec §3.5 says it must not"

    async def test_a_live_archive_means_no_new_row(self, db_session, printer_factory, main_db):
        """Dispatched before the restart, or adopted in an earlier session: the
        row already exists and the hook behaves exactly as it did before."""
        _pid, rows, spies, spawned = await _drive_hook(
            db_session=db_session, printer_factory=printer_factory, live_archive=(42, 1)
        )

        assert rows == [], "a print that already had an archive must not get a second one"
        spies["queue_claim"].assert_awaited_once()
        assert spies["queue_claim"].await_args.kwargs["archive_id"] == 42
        assert spawned.names == [], "nothing to download — the live archive owns its own file"

    async def test_adoption_failure_never_breaks_the_hook(self, db_session, printer_factory, main_db):
        """The hook runs on the connect path. An adoption that cannot finish is
        logged and the timelapse baseline — which has no second chance, the
        printer uploads the in-flight MP4 at completion — still happens."""
        boom = AsyncMock(side_effect=RuntimeError("no queue for you"))
        _pid, rows, spies, spawned = await _drive_hook(
            db_session=db_session,
            printer_factory=printer_factory,
            extra_patches=[patch("backend.app.main._default_queue_id_for_printer", boom)],
        )

        assert rows == [], "a half-built row is worse than none"
        # The hook must not have given up before its own job.
        spies["baseline"].assert_awaited_once()
        # The old rule survives the failure: no archive ⇒ no queue claim, or the
        # completion finds a row it can neither close nor repeat.
        spies["queue_claim"].assert_not_awaited()
        assert spawned.names == []
        for name in SILENT:
            assert not spies[name].called

    async def test_a_print_already_tracked_is_not_adopted_twice(self, db_session, printer_factory, main_db):
        """⚠️ ``_live_archive_for_running_print`` inspects only the 5 newest
        ``printing`` rows on the printer, so a miss is not proof there is no row.

        Before the adoption existed, such a miss cost nothing but a skipped
        queue claim; now it would cut a SECOND archive for a print BamDude is
        already tracking. ``_active_prints`` is the same guard
        ``on_print_start`` keeps at the top of its handler, and it is the one
        thing that survives a lookup window too small.
        """
        from backend.app.main import _active_prints

        _pid, rows, spies, spawned = await _drive_hook(
            db_session=db_session,
            printer_factory=printer_factory,
            pre_seed=lambda pid: _active_prints.__setitem__((pid, "Кронштейн"), 4242),
        )

        assert rows == [], "the print is already tracked as archive 4242 — a second row is a duplicate"
        spies["queue_claim"].assert_not_awaited()
        spies["energy_start"].assert_not_awaited()
        assert spawned.names == [], "and no second FTP session for a file already accounted for"

    async def test_the_key_the_adoption_itself_writes_is_a_guard_key_too(self, db_session, printer_factory, main_db):
        """The printer reports no ``filename`` and a ``subtask_name`` carrying the
        ``.gcode.3mf`` suffix — so the row the earlier adoption created is named
        ``Кронштейн.3mf`` and registered under that key. None of the forms built
        from ``filename``/``subtask_name`` is that string, so the guard has to
        derive the row's own name form as well, or the adoption cannot see the
        row it wrote itself.
        """
        from backend.app.main import _active_prints

        _pid, rows, spies, spawned = await _drive_hook(
            db_session=db_session,
            printer_factory=printer_factory,
            data={**RUNNING, "filename": "", "subtask_name": "Кронштейн.gcode.3mf"},
            pre_seed=lambda pid: _active_prints.__setitem__((pid, "Кронштейн.3mf"), 4242),
        )

        assert rows == [], "already tracked as 4242 under the name the adoption writes — a second row is a duplicate"
        spies["queue_claim"].assert_not_awaited()
        spies["energy_start"].assert_not_awaited()
        assert spawned.names == []


def _provisional(printer_id: int, *, plate: int | None = 1) -> PrintArchive:
    """The row the adoption created: named from MQTT, nothing on disk yet."""
    return PrintArchive(
        printer_id=printer_id,
        filename="/data/Metadata/plate_1.gcode",
        file_path="",
        file_size=0,
        print_name="Кронштейн",
        status="printing",
        plate_index=plate,
        extra_data={
            "original_subtask": "Кронштейн",
            # A real mapping: the attach tail must take it from HERE (the row's
            # own record of the print), not invent one from what is loaded now.
            "_print_data": {**RUNNING, "ams_mapping": [0, 1]},
            "recovered_start": {"observed_at": datetime.now(timezone.utc).isoformat(), "remaining_seconds": 1800},
            "energy_is_approximate": True,
            "energy_start_partial": True,
        },
    )


async def _seed_download_case(
    db_session, printer_factory, tmp_path, *, with_twin: bool, twin_energy: float | None = None
):
    """A provisional row (+ optionally the live twin holding the same bytes) and
    the temp file the download would have produced."""
    from backend.app.main import _active_prints

    printer = await printer_factory()
    temp_path: Path = tmp_path / "p.3mf"
    temp_path.write_bytes(b"PK\x03\x04 the same bytes the printer has")
    content_hash = ArchiveService.compute_file_hash(temp_path)

    twin_id = None
    if with_twin:
        twin = PrintArchive(
            printer_id=printer.id,
            filename="named-before-the-restart.3mf",
            file_path="archive/old/named-before-the-restart.3mf",
            file_size=temp_path.stat().st_size,
            print_name="named before the restart",
            content_hash=content_hash,
            source_content_hash=content_hash,
            plate_index=1,
            status="printing",
            started_at=datetime.now(timezone.utc),
            energy_start_kwh=twin_energy,
        )
        db_session.add(twin)

    provisional = _provisional(printer.id)
    db_session.add(provisional)
    await db_session.commit()
    await db_session.refresh(provisional)
    if with_twin:
        await db_session.refresh(twin)
        twin_id = twin.id

    # ⚠️ Ints from here on: the tests ``expire_all()`` before asserting, and an
    # expired ORM attribute is a lazy load with no greenlet to run it in.
    printer_id, provisional_id = printer.id, provisional.id
    _active_prints[(printer_id, provisional.filename)] = provisional_id
    _active_prints[(printer_id, "Кронштейн")] = provisional_id
    _active_prints[(printer_id, "Кронштейн.3mf")] = provisional_id

    return printer_id, provisional_id, twin_id, temp_path, content_hash


async def _run_download(*, printer_id, archive_id, download_result=None, download_error=None, attach=None):
    """Run ``_download_for_adopted_print`` with the FTP fetch stubbed.

    Returns the spies for everything the task is supposed to drive, so a test
    can assert on the whole tail and not just on the attach. ``download_error``
    makes the fetch raise instead of answering.
    """
    from contextlib import ExitStack

    from backend.app.main import _download_for_adopted_print

    download = (
        AsyncMock(side_effect=download_error) if download_error is not None else AsyncMock(return_value=download_result)
    )
    spies = {
        "attach": AsyncMock(return_value=True) if attach is None else attach,
        "colors": AsyncMock(),
        "spoolman": AsyncMock(),
        "energy_start": AsyncMock(return_value=True),
        "parts": AsyncMock(),
        "objects": MagicMock(return_value=True),
        "ws_updated": AsyncMock(),
    }
    patches = [
        patch("backend.app.services.archive_download.try_download_3mf", download),
        patch.object(ArchiveService, "attach_3mf_to_archive", spies["attach"]),
        patch("backend.app.main.ws_manager", MagicMock(send_archive_updated=spies["ws_updated"])),
        patch("backend.app.main.refresh_archive_parts", spies["parts"]),
        patch("backend.app.main._record_energy_start", spies["energy_start"]),
        patch("backend.app.main._store_spoolman_print_data", spies["spoolman"]),
        patch("backend.app.services.archive_colors.apply_loaded_spool_colors", spies["colors"]),
        patch("backend.app.services.archive.load_objects_from_archive_into_state", spies["objects"]),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await _download_for_adopted_print(printer_id, archive_id, logging.getLogger("test"))
    return spies


class TestTheDownloadDecidesWhoOwnsThePrint:
    async def test_a_hash_twin_before_the_attach_wins(self, db_session, printer_factory, main_db, tmp_path):
        """The bytes prove the print already had a row. Our guess goes away —
        and it can only go away because nothing was ever written under it,
        which is why the hash is taken BEFORE the attach."""
        from backend.app.main import _active_prints

        printer_id, provisional_id, twin_id, temp_path, _hash = await _seed_download_case(
            db_session, printer_factory, tmp_path, with_twin=True
        )

        spies = await _run_download(
            printer_id=printer_id, archive_id=provisional_id, download_result=(temp_path, "p.3mf")
        )

        assert not spies["attach"].called, (
            "attached before asking — _discard_provisional_archive may only delete a row with nothing on disk"
        )
        db_session.expire_all()
        rows = (await db_session.execute(select(PrintArchive.id).order_by(PrintArchive.id))).scalars().all()
        assert rows == [twin_id], f"one physical print, archives {rows}"
        assert set(_active_prints.values()) == {twin_id}, (
            f"a key still points at the deleted row: {_active_prints}; the duplicate guard reads that as "
            f"'already tracked' and the next event returns on it"
        )
        assert _active_prints.get((printer_id, "Кронштейн")) == twin_id
        assert _active_prints.get((printer_id, "Кронштейн.3mf")) == twin_id
        assert not temp_path.exists(), "the temp file outlived the download"
        # The twin is the one path that does NOT get a tracking row from here:
        # it has had its own since it was dispatched, and the row we would have
        # named no longer exists.
        assert not spies["spoolman"].called

    async def test_no_twin_attaches_the_file(self, db_session, printer_factory, main_db, tmp_path):
        printer_id, provisional_id, _twin, temp_path, _hash = await _seed_download_case(
            db_session, printer_factory, tmp_path, with_twin=False
        )

        spies = await _run_download(
            printer_id=printer_id, archive_id=provisional_id, download_result=(temp_path, "p.3mf")
        )

        spies["attach"].assert_awaited_once_with(provisional_id, temp_path, "p.3mf")
        db_session.expire_all()
        rows = (await db_session.execute(select(PrintArchive.id))).scalars().all()
        assert rows == [provisional_id], "the row that was announced at adoption is the row that gets the file"
        assert not temp_path.exists(), "the temp file outlived the download"

    async def test_a_failed_download_marks_the_row(self, db_session, printer_factory, main_db, tmp_path):
        """⚠️ ``no_3mf_available`` means "we tried and could not" — it raises a
        user-facing banner, so it is set only after an attempt has failed."""
        printer_id, provisional_id, _twin, _temp, _hash = await _seed_download_case(
            db_session, printer_factory, tmp_path, with_twin=False
        )

        spies = await _run_download(printer_id=printer_id, archive_id=provisional_id, download_result=None)

        assert not spies["attach"].called
        assert not spies["colors"].called, "nothing was attached — there are no slicer colours to outrank"
        # ⚠️ The tracking row follows the download's OUTCOME, not the attach's
        # success. ``on_print_start`` calls ``store_print_data`` whatever its own
        # 3MF did, and the print here is running just the same: with the row's
        # empty ``file_path`` the store takes its degraded remain%-delta path —
        # which is the reason the call waits for the download, not a reason for a
        # Spoolman install to get no row at all.
        spies["spoolman"].assert_awaited_once()
        assert spies["spoolman"].await_args.args[1] == provisional_id
        assert spies["spoolman"].await_args.args[2] == "", "the empty file_path is what selects the degraded path"
        assert spies["spoolman"].await_args.kwargs["ams_mapping"] == [0, 1], (
            "the mapping comes from the row's own ``_print_data``"
        )
        db_session.expire_all()
        row = await db_session.get(PrintArchive, provisional_id)
        assert row is not None
        assert (row.extra_data or {}).get("no_3mf_available") is True
        assert row.file_path == "", "the retry triggers still have to see it"
        assert (row.extra_data or {}).get("recovered_start") is not None, (
            "the marker must not overwrite the record the reconstruction reads"
        )

    async def test_a_failed_attach_keeps_the_row_and_still_tracks_the_print(
        self, db_session, printer_factory, main_db, tmp_path
    ):
        """The bytes arrived and could not be attached. The row keeps its empty
        ``file_path`` so the four retry triggers come back to it — and the
        Spoolman row is written anyway, for the same reason a failed download
        gets one: the print is running either way."""
        printer_id, provisional_id, _twin, temp_path, _hash = await _seed_download_case(
            db_session, printer_factory, tmp_path, with_twin=False
        )

        spies = await _run_download(
            printer_id=printer_id,
            archive_id=provisional_id,
            download_result=(temp_path, "p.3mf"),
            attach=AsyncMock(return_value=False),
        )

        spies["attach"].assert_awaited_once()
        assert not spies["colors"].called, "nothing was attached — there are no slicer colours to outrank"
        assert not spies["objects"].called, "and no objects to put into state"
        spies["spoolman"].assert_awaited_once()
        assert spies["spoolman"].await_args.args[2] == "", "the row never got a file — the degraded path again"
        db_session.expire_all()
        row = await db_session.get(PrintArchive, provisional_id)
        assert row is not None
        assert row.file_path == "", "a failed attach must not read as a finished one"
        assert not temp_path.exists(), "the temp file outlived the download"

    async def test_a_download_that_raises_is_contained(self, db_session, printer_factory, main_db, tmp_path, caplog):
        """The task runs unattended on the connect path, so its own outer
        ``except`` is the only thing between a bug in it and a traceback nobody
        reads.

        ⚠️ The row comes out untouched — in particular WITHOUT
        ``no_3mf_available``: that flag means "we asked the printer and it had
        nothing", and it raises a user-facing banner. An error on our side is
        not that answer. Nor is it an outcome the tracking row can follow, so
        the Spoolman store — which every real outcome gets — is not reached.
        """
        printer_id, provisional_id, _twin, _temp, _hash = await _seed_download_case(
            db_session, printer_factory, tmp_path, with_twin=False
        )

        with caplog.at_level(logging.ERROR, logger="test"):
            spies = await _run_download(
                printer_id=printer_id,
                archive_id=provisional_id,
                download_error=RuntimeError("the FTP socket died mid-LIST"),
            )

        assert not spies["attach"].called
        assert not spies["spoolman"].called
        assert any(record.levelno >= logging.ERROR for record in caplog.records), (
            "a swallowed exception that is not logged is an invisible one"
        )
        db_session.expire_all()
        row = await db_session.get(PrintArchive, provisional_id)
        assert row is not None
        assert row.file_path == "", "the row still waits for its file"
        assert (row.extra_data or {}).get("no_3mf_available") is None, (
            "the banner says 'the printer has no 3MF for this print' — an error of ours must not raise it"
        )
        assert row.status == "printing"


class TestTheAdoptedAttachMirrorsOnPrintStartsTail:
    """Spec §3.4 as amended 2026-09-12: after the adopted row gets its file the
    task does what ``on_print_start`` does after ITS attach — not what the retry
    service does. The retry service fills a row somebody else already wired up;
    this task owns the print end to end, and the two things it would otherwise
    drop are both silent losses.
    """

    async def test_the_loaded_spools_colours_outrank_the_slicers(self, db_session, printer_factory, main_db, tmp_path):
        """``attach_3mf_to_archive`` has just overwritten ``filament_color`` with
        the slicer's own, and a loaded inventory spool outranks those — the same
        order the dispatch path applies them in."""
        printer_id, provisional_id, _twin, temp_path, _hash = await _seed_download_case(
            db_session, printer_factory, tmp_path, with_twin=False
        )

        spies = await _run_download(
            printer_id=printer_id, archive_id=provisional_id, download_result=(temp_path, "p.3mf")
        )

        spies["colors"].assert_awaited_once()
        args = spies["colors"].await_args.args
        assert args[1].id == provisional_id, "the colours were re-applied to some other row"
        assert args[2] == printer_id
        assert args[3] == [0, 1], (
            "the mapping must come from the row's own ``_print_data``, not from whatever is loaded now"
        )

    async def test_spoolman_gets_its_tracking_row_once_the_file_is_there(
        self, db_session, printer_factory, main_db, tmp_path
    ):
        """⚠️ The ``CLAUDE.md`` rule is about TIMING, not omission: with an empty
        ``file_path`` ``store_print_data`` takes its degraded remain%-delta path,
        so the call waits for the attach — and then has to happen, or a Spoolman
        install gets no row at all for an adopted print."""
        printer_id, provisional_id, _twin, temp_path, _hash = await _seed_download_case(
            db_session, printer_factory, tmp_path, with_twin=False
        )

        spies = await _run_download(
            printer_id=printer_id, archive_id=provisional_id, download_result=(temp_path, "p.3mf")
        )

        spies["spoolman"].assert_awaited_once()
        call = spies["spoolman"].await_args
        assert call.args[0] == printer_id
        assert call.args[1] == provisional_id
        assert call.kwargs["ams_mapping"] == [0, 1]
        assert call.kwargs["plate_id"] == 1, "plate authority is PrintArchive.plate_index"

    async def test_the_adopted_twin_keeps_an_energy_baseline(self, db_session, printer_factory, main_db, tmp_path):
        """The provisional row's plug reading dies with the row, and the twin was
        created before BamDude could take one — so without this the print's
        energy figure is lost rather than merely approximate."""
        printer_id, provisional_id, twin_id, temp_path, _hash = await _seed_download_case(
            db_session, printer_factory, tmp_path, with_twin=True
        )

        spies = await _run_download(
            printer_id=printer_id, archive_id=provisional_id, download_result=(temp_path, "p.3mf")
        )

        spies["energy_start"].assert_awaited_once()
        assert spies["energy_start"].await_args.args[0].id == twin_id
        assert spies["energy_start"].await_args.kwargs["context"] == "hash-adoption"

    async def test_a_twin_that_already_has_a_baseline_is_left_alone(
        self, db_session, printer_factory, main_db, tmp_path
    ):
        """Re-reading the counter mid-print would move the baseline forward and
        silently discard every watt-hour drawn before now."""
        printer_id, provisional_id, _twin, temp_path, _hash = await _seed_download_case(
            db_session, printer_factory, tmp_path, with_twin=True, twin_energy=12.5
        )

        spies = await _run_download(
            printer_id=printer_id, archive_id=provisional_id, download_result=(temp_path, "p.3mf")
        )

        spies["energy_start"].assert_not_awaited()
