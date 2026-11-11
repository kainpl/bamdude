"""F1 source proof comes from the writer's parsed bytes, never a donor row."""

import threading
import zipfile

import pytest

from backend.app.models.archive import PrintArchive
from backend.app.services.archive import ArchiveService
from backend.app.services.layer_source import MARKER, select_layer_total


def sliced(path, *, patched=False, hidden_header=False):
    with zipfile.ZipFile(path, "w") as z:
        for index, total in [(1, 693), (2, 91)]:
            prefix = "; padding\n" * 600 if hidden_header else ""
            # Equal fixture contents must have equal hashes across ZIP's
            # two-second timestamp boundary, including on slow CI workers.
            entry = zipfile.ZipInfo(f"Metadata/plate_{index}.gcode", date_time=(2026, 1, 1, 0, 0, 0))
            z.writestr(
                entry,
                prefix + f"; total layer number: {total}\n" + ("; M970\n" if patched else "M970\n"),
            )
    return path


@pytest.fixture
def storage(tmp_path, monkeypatch):
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(settings, "archive_dir", tmp_path / "archive")
    (tmp_path / "archive").mkdir()
    return tmp_path


async def dispatch(db, printer_id, storage, *, hidden_header=False):
    source = sliced(storage / "source.3mf")
    upload = sliced(storage / "upload.3mf", patched=True, hidden_header=hidden_header)
    archive = await ArchiveService(db).archive_print(
        printer_id=printer_id,
        source_file=source,
        dispatched_file=upload,
        source_content_hash=ArchiveService.compute_file_hash(source),
        applied_patches=["mesh_mode_fast_check_off"],
        plate_index=1,
        print_data={"status": "printing"},
    )
    assert archive.content_hash != archive.source_content_hash
    return archive, upload


async def test_f01_f17b_patched_dispatch_and_dedup_stamp_their_own_row(db_session, printer_factory, storage):
    printer = await printer_factory()
    first, _ = await dispatch(db_session, printer.id, storage)
    second, _ = await dispatch(db_session, printer.id, storage)
    assert first.id != second.id and first.file_path == second.file_path
    for row in (first, second):
        assert select_layer_total(row, 1) == 693
        assert select_layer_total(row, 2) == 91
        assert select_layer_total(row, 3) is None
        assert row.extra_data[MARKER]["content_hash"] == row.content_hash
        assert row.extra_data[MARKER]["source_content_hash"] == row.source_content_hash


@pytest.mark.parametrize("hidden_header", [False, True])
async def test_f17c_attach_uses_prepared_download_even_when_chain_root_is_reused(
    db_session, printer_factory, storage, monkeypatch, hidden_header
):
    import backend.app.services.archive as module

    printer = await printer_factory()
    donor, upload = await dispatch(db_session, printer.id, storage, hidden_header=hidden_header)
    target = PrintArchive(
        printer_id=printer.id, filename="external.3mf", file_path="", file_size=0, plate_index=1, status="printing"
    )
    db_session.add(target)
    await db_session.commit()
    # The fresh parse must be the worker's existing parse, not a new ZIP read
    # of the unpatched donor under the transaction lock.
    original = module.parse_plates_from_3mf
    calls = []
    loop_thread = threading.get_ident()

    def parsed(z):
        calls.append(threading.get_ident())
        return original(z)

    monkeypatch.setattr(module, "parse_plates_from_3mf", parsed)
    assert await ArchiveService(db_session).attach_3mf_to_archive(target.id, upload, "external.3mf")
    await db_session.refresh(target)
    assert calls and all(thread != loop_thread for thread in calls)
    assert len(calls) == 1
    assert target.file_path == donor.file_path
    assert target.content_hash == donor.content_hash
    assert target.source_content_hash == donor.source_content_hash
    assert select_layer_total(donor, 1) == 693
    assert select_layer_total(target, 1) == (None if hidden_header else 693)


async def test_f17_failed_fresh_plate_parse_clears_old_cache_and_reserved_marker(
    db_session, printer_factory, storage, monkeypatch
):
    import backend.app.services.archive as module

    printer = await printer_factory()
    row, _ = await dispatch(db_session, printer.id, storage)
    row.extra_data = {**row.extra_data, "is_multi_plate": True}
    await db_session.commit()
    replacement = sliced(storage / "replacement.3mf", patched=True, hidden_header=True)

    def broken(_):
        raise ValueError("injected per-plate failure")

    monkeypatch.setattr(module, "parse_plates_from_3mf", broken)
    assert await ArchiveService(db_session).attach_3mf_to_archive(row.id, replacement, "replacement.3mf")
    await db_session.refresh(row)
    assert select_layer_total(row, 1) is None
    assert "plates" not in row.extra_data and "is_multi_plate" not in row.extra_data
    assert row.extra_data[MARKER]["plates"] == {}


async def test_f17_rollback_keeps_the_committed_source_proof(db_session, printer_factory, storage, monkeypatch):
    from copy import deepcopy

    printer = await printer_factory()
    row, _ = await dispatch(db_session, printer.id, storage)
    before = deepcopy(row.extra_data[MARKER])
    replacement = sliced(storage / "rollback.3mf", patched=True, hidden_header=True)
    original_commit = db_session.commit
    commits = 0

    async def fail_publication():
        nonlocal commits
        commits += 1
        if commits == 2:
            raise RuntimeError("injected commit failure")
        await original_commit()

    monkeypatch.setattr(db_session, "commit", fail_publication)
    assert not await ArchiveService(db_session).attach_3mf_to_archive(row.id, replacement, "rollback.3mf")
    await db_session.refresh(row)
    assert row.extra_data[MARKER] == before
    assert select_layer_total(row, 1) == 693
