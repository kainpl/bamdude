"""Regression test: archive_print with dispatched_file uses the patched bytes for content_hash.

Before the fix the dispatcher passed only ``source_file=upload_file_path`` (or
the unpatched path, depending on which version of the code) and ``content_hash``
ended up reflecting whichever single file got passed. With patches applied
that broke the on_print_start restart-recovery path: it downloads the bytes
from the printer (= patched) and looks for ``content_hash == hash(printer_copy)``,
which never matched archives that recorded the unpatched library hash.

Post-fix:

* ``content_hash`` = SHA256(``dispatched_file``) — what's on the printer
* ``source_content_hash`` = SHA256(``source_file``) (or the explicit param) — chain root
* When ``dispatched_file`` is None the two coincide (legacy single-file path)

The on-disk archive copy is the dispatched bytes (so later ZipFile reads see
exactly the bytes the printer received), and cross-printer file dedup keys on
``content_hash`` so 6 prints with the same patches share one on-disk file.
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

    # The on-disk copy is the dispatched bytes (so later ZipFile reads see
    # exactly what the printer got). Verify by hashing the file at file_path.
    on_disk = tmp_path / archive.file_path
    assert on_disk.exists()
    h = hashlib.sha256()
    with open(on_disk, "rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            h.update(chunk)
    assert h.hexdigest() == dispatched_hash


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
