"""Integration tests for source_content_hash / applied_patches on PrintArchive.

Covers the three scenarios from the v1 dedup mechanism:

1. Dispatch-initiated `archive_print()` with `source_content_hash` persists
   the field and uses it for dedup against future matching archives.
2. Two archives with divergent `content_hash` but identical
   `source_content_hash` dedup together via COALESCE.
3. External-print fallback: when the caller doesn't provide source hash but
   the computed content hash matches an existing chain, the new archive
   inherits the chain's effective hash.
"""

from pathlib import Path

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.services.archive import ArchiveService


def _make_tempfile(tmp_path: Path, name: str, payload: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(payload)
    return p


@pytest.mark.asyncio
@pytest.mark.integration
async def test_archive_print_persists_source_hash_and_patches(db_session, printer_factory, tmp_path):
    """`archive_print()` writes source_content_hash + applied_patches when supplied."""
    printer = await printer_factory()
    src = _make_tempfile(tmp_path, "design.3mf", b"patched-bytes-v1")

    service = ArchiveService(db_session)
    archive = await service.archive_print(
        printer_id=printer.id,
        source_file=src,
        original_filename="design.3mf",
        source_content_hash="deadbeef" * 8,  # 64-char pseudo SHA256
        applied_patches=["vibration_fast_check_off"],
    )

    assert archive is not None
    assert archive.source_content_hash == "deadbeef" * 8
    # Stored as JSON text — decode and confirm.
    import json as _json

    assert _json.loads(archive.applied_patches) == ["vibration_fast_check_off"]
    # content_hash is the hash of the actual bytes we passed in; MUST differ
    # from source hash (that's the whole point of the mechanism).
    assert archive.content_hash is not None
    assert archive.content_hash != archive.source_content_hash


@pytest.mark.asyncio
@pytest.mark.integration
async def test_coalesce_dedup_across_different_content_hashes(db_session, printer_factory, tmp_path):
    """Two patched archives with same source dedup via COALESCE even when
    content_hash differs between them (e.g. different patch set or repeat)."""
    printer = await printer_factory()
    shared_source_hash = "cafef00d" * 8

    # First dispatch: one flavour of patched bytes.
    src_a = _make_tempfile(tmp_path, "a.3mf", b"patched-A")
    a = await ArchiveService(db_session).archive_print(
        printer_id=printer.id,
        source_file=src_a,
        original_filename="design.3mf",
        source_content_hash=shared_source_hash,
    )
    # Second dispatch: different patched bytes, same unpatched source.
    src_b = _make_tempfile(tmp_path, "b.3mf", b"patched-B")
    b = await ArchiveService(db_session).archive_print(
        printer_id=printer.id,
        source_file=src_b,
        original_filename="design.3mf",
        source_content_hash=shared_source_hash,
    )

    assert a is not None and b is not None
    assert a.content_hash != b.content_hash
    # Both rows carry the same source_content_hash → COALESCE(source,content)
    # resolves to the same value for both → dedup sees them as the same chain.
    assert a.source_content_hash == b.source_content_hash == shared_source_hash

    # Verify `get_duplicate_hashes_and_names` reports the shared chain as
    # a duplicate (it groups by COALESCE(source, content)).
    dup_hashes, _pairs = await ArchiveService(db_session).get_duplicate_hashes_and_names()
    assert shared_source_hash in dup_hashes


@pytest.mark.asyncio
@pytest.mark.integration
async def test_external_print_inherits_chain_from_prior_archive(db_session, printer_factory, tmp_path):
    """External print (no source_content_hash in call) should inherit the
    chain when its file bytes match some existing archive's content_hash."""
    printer = await printer_factory()
    original_source_hash = "facefeed" * 8

    # Step 1 — BamDude dispatch writes an archive with a known chain.
    bamdude_bytes = b"patched-file-on-sd"
    src_dispatch = _make_tempfile(tmp_path, "dispatch.3mf", bamdude_bytes)
    dispatched = await ArchiveService(db_session).archive_print(
        printer_id=printer.id,
        source_file=src_dispatch,
        original_filename="design.3mf",
        source_content_hash=original_source_hash,
    )
    assert dispatched is not None
    patched_content_hash = dispatched.content_hash

    # Step 2 — External print (user pressed "print" on printer screen) returns
    # the SAME patched bytes still sitting on SD. `archive_print()` is called
    # WITHOUT source_content_hash — the fallback lookup should attach this
    # new row to the existing chain.
    src_external = _make_tempfile(tmp_path, "external.3mf", bamdude_bytes)
    external = await ArchiveService(db_session).archive_print(
        printer_id=printer.id,
        source_file=src_external,
        original_filename="design.3mf",
        # NO source_content_hash — caller doesn't know
    )

    assert external is not None
    # Because content matches dispatched.content_hash, external archive
    # picks up the chain's effective hash as its own source_content_hash.
    assert external.source_content_hash == original_source_hash
    # Both rows now resolve to the same effective hash.
    assert external.content_hash == patched_content_hash


@pytest.mark.asyncio
@pytest.mark.integration
async def test_external_print_without_chain_stays_null(db_session, printer_factory, tmp_path):
    """External print whose bytes don't match any prior chain keeps
    source_content_hash NULL — it's a genuinely foreign file."""
    printer = await printer_factory()
    src = _make_tempfile(tmp_path, "foreign.3mf", b"never-seen-before-bytes")

    archive = await ArchiveService(db_session).archive_print(
        printer_id=printer.id,
        source_file=src,
        original_filename="foreign.3mf",
    )

    assert archive is not None
    assert archive.source_content_hash is None
    assert archive.applied_patches is None

    # Sanity: stored row matches what we inserted.
    row = (await db_session.execute(select(PrintArchive).where(PrintArchive.id == archive.id))).scalar_one()
    assert row.source_content_hash is None
