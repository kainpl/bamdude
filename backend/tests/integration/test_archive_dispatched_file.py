"""Regression test: archive_print splits hashes between dispatched + source.

* ``content_hash`` = SHA256(``dispatched_file``) — what's on the printer.
  Drives ``on_print_start``'s restart-recovery query (it pulls the printer's
  copy back over FTP, hashes it, looks for ``content_hash == temp_hash``).
* ``source_content_hash`` = SHA256(``source_file``) (or the explicit param) —
  chain root. Drives chain-of-custody grouping + file-on-disk dedup.
* On-disk file at ``file_path`` is the **unpatched original** (``source_file``).
  Reprint reads this back and re-runs the patcher per-job, so toggling
  ``mesh_mode_fast_check`` / gcode injection on a reprint actually takes
  effect — the patcher's M970 regex only matches uncommented lines and
  couldn't undo a previously-baked patch.
* When ``dispatched_file`` is None the two hashes coincide (legacy
  single-file path: external prints, VP file_manager save, library upload
  without patching).

Cross-printer file dedup keys on ``effective_hash =
COALESCE(source_content_hash, content_hash)``: every row sharing an
unpatched origin shares the same on-disk file, regardless of which patches
each individual dispatch applied.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from backend.app.services.archive import ArchiveService


def _write_zip(path: Path, payload: bytes) -> str:
    """Write a tiny valid ZIP at ``path`` with ``payload`` inside; return SHA256."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/model_settings.config", payload.decode("utf-8", errors="replace"))
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_archive_print_with_dispatched_file_records_patched_hash_as_content(
    db_session, tmp_path, monkeypatch, printer_factory
):
    """Patched dispatched bytes → content_hash = patched, source_content_hash = unpatched."""
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    (tmp_path / "archive").mkdir(parents=True, exist_ok=True)

    # Two distinct ZIPs with different bytes → different sha256s.
    src = tmp_path / "library_original.3mf"
    src_hash = _write_zip(src, b"<config name='unpatched'/>")

    dispatched = tmp_path / "tmp_patched_for_upload.3mf"
    dispatched_hash = _write_zip(dispatched, b"<config name='patched-mesh-mode-off'/>")

    assert src_hash != dispatched_hash, "test setup invalid — payloads must differ"

    printer = await printer_factory()

    service = ArchiveService(db_session)
    archive = await service.archive_print(
        printer_id=printer.id,
        source_file=src,
        dispatched_file=dispatched,
        original_filename="library_original.3mf",
        source_content_hash=src_hash,  # explicit chain root
        applied_patches=["mesh_mode_fast_check_off"],
        print_data={"status": "printing"},
    )

    assert archive is not None
    # content_hash reflects what's going to the printer (the patched bytes).
    assert archive.content_hash == dispatched_hash
    # source_content_hash carries the chain root — the original library bytes.
    assert archive.source_content_hash == src_hash
    # And those two MUST differ — that's the whole point of the split.
    assert archive.content_hash != archive.source_content_hash

    # The on-disk copy is the UNPATCHED source (so reprint can re-run the
    # patcher against a clean source and toggle patches in either direction).
    # ``content_hash`` stays as the FTP/restart-recovery key — those two
    # roles intentionally don't share a file. Verify by hashing the file at
    # file_path: it must equal the source hash, NOT the dispatched hash.
    on_disk = tmp_path / archive.file_path
    assert on_disk.exists()
    h = hashlib.sha256()
    with open(on_disk, "rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            h.update(chunk)
    assert h.hexdigest() == src_hash, "on-disk copy must be the unpatched source, not the dispatched bytes"
    assert h.hexdigest() != dispatched_hash, "patched dispatched bytes must NOT land on disk"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_archive_print_without_dispatched_file_legacy_single_path(
    db_session, tmp_path, monkeypatch, printer_factory
):
    """No dispatched_file → content_hash = SHA256(source_file) — legacy invariant."""
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    (tmp_path / "archive").mkdir(parents=True, exist_ok=True)

    src = tmp_path / "external_print.3mf"
    src_hash = _write_zip(src, b"<config name='external'/>")

    printer = await printer_factory()

    service = ArchiveService(db_session)
    archive = await service.archive_print(
        printer_id=printer.id,
        source_file=src,
        # dispatched_file omitted — legacy callers (external prints, VP
        # file_manager save, library upload without patching) keep working.
        print_data={"status": "completed"},
    )

    assert archive is not None
    assert archive.content_hash == src_hash
    # source_content_hash falls back to content_hash via chain_lookup → standalone-row case.
    assert archive.source_content_hash == src_hash


@pytest.mark.asyncio
@pytest.mark.integration
async def test_archive_print_dispatched_file_dedup_shares_on_disk_copy(
    db_session, tmp_path, monkeypatch, printer_factory
):
    """6 prints with the same patches share one on-disk file (cross-printer dedup keys on content_hash)."""
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    (tmp_path / "archive").mkdir(parents=True, exist_ok=True)

    src = tmp_path / "src.3mf"
    src_hash = _write_zip(src, b"<config name='unpatched'/>")

    # Each "dispatch" rebuilds the patched temp file from scratch (the dispatcher
    # does this via apply_3mf_transforms per job). Same patcher input + same patch
    # set → same bytes → same hash → same on-disk file.
    def _make_patched() -> Path:
        p = tmp_path / f"patched_{len(list(tmp_path.glob('patched_*.3mf')))}.3mf"
        _write_zip(p, b"<config name='patched-mesh-mode-off'/>")
        return p

    p1 = await printer_factory(name="P1")
    p2 = await printer_factory(name="P2")

    service = ArchiveService(db_session)
    a1 = await service.archive_print(
        printer_id=p1.id,
        source_file=src,
        dispatched_file=_make_patched(),
        source_content_hash=src_hash,
        print_data={"status": "printing"},
    )
    a2 = await service.archive_print(
        printer_id=p2.id,
        source_file=src,
        dispatched_file=_make_patched(),
        source_content_hash=src_hash,
        print_data={"status": "printing"},
    )

    assert a1.content_hash == a2.content_hash
    # Cross-printer dedup: both archives point at the SAME file_path.
    assert a1.file_path == a2.file_path
