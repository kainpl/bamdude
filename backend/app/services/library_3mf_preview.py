"""Preview injection for library-sliced 3MFs whose source was a bare STL.

When the slicer sidecar (Bambu Studio / OrcaSlicer CLI) is fed a bare
``.stl``, the resulting ``.gcode.3mf`` carries **no** ``Metadata/plate_1.png``
— the desktop slicer renders previews from its own GUI; the CLI cannot.
Without injection the library card for the sliced row shows an empty
preview tile.

Strategy (per user spec):

1. If the sliced 3MF already carries any of the canonical preview slots,
   pass through unchanged (slicer's render wins when present).
2. Otherwise, when the source LibraryFile is an STL:
   a) reuse the source STL's ``thumbnail_path`` PNG if it exists
      (upload already renders one via :func:`generate_stl_thumbnail`
      by default), OR
   b) generate one now from the STL on disk and persist it on the
      source row (so the source STL itself lights up in the library
      listing too — matches user instruction "додаєм і до стл").
3. Rewrite the 3MF zip with the PNG embedded under all three canonical
   preview slots (``plate_1.png`` / ``top_1.png`` / ``pick_1.png``) so
   any consumer that looks at any of them picks it up.

Out of scope for now — STEP / OBJ / 3MF source files. STEP has no upload
thumbnail pipeline; OBJ runs through the same trimesh path as STL but
is rare in practice; 3MF sources already pass their own preview through
the sidecar.

**Hash invariant:** the caller MUST re-compute ``file_hash`` over the
returned bytes — the injected zip is the canonical disk content.
"""

from __future__ import annotations

import io
import logging
import zipfile

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.library import LibraryFile

logger = logging.getLogger(__name__)

# Canonical preview slots in a Bambu 3MF (matches calib_thumbnail.py +
# archive.ThreeMFParser._extract_thumbnail lookup order).
_PREVIEW_NAMES = (
    "Metadata/plate_1.png",
    "Metadata/top_1.png",
    "Metadata/pick_1.png",
)


def _has_3mf_preview(content: bytes) -> bool:
    """Return True iff the 3MF already carries any canonical preview PNG."""
    try:
        with zipfile.ZipFile(io.BytesIO(content), "r") as zf:
            names = set(zf.namelist())
    except zipfile.BadZipFile:
        return False
    return any(name in names for name in _PREVIEW_NAMES)


def _inject_preview(content: bytes, png_bytes: bytes) -> bytes:
    """Embed ``png_bytes`` into the 3MF under every preview slot.

    Existing entries at preview paths are overwritten; missing slots are
    added. Every other zip member passes through unchanged. Falls back to
    the original bytes on any zip / IO error so a corrupt sidecar output
    never blocks the slice from landing in the library.
    """
    try:
        out = io.BytesIO()
        with (
            zipfile.ZipFile(io.BytesIO(content), "r") as src,
            zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst,
        ):
            written: set[str] = set()
            for name in src.namelist():
                if name in _PREVIEW_NAMES:
                    dst.writestr(name, png_bytes)
                    written.add(name)
                else:
                    dst.writestr(name, src.read(name))
            for name in _PREVIEW_NAMES:
                if name not in written:
                    dst.writestr(name, png_bytes)
        return out.getvalue()
    except (zipfile.BadZipFile, OSError) as exc:
        logger.warning("library_3mf_preview: inject failed (%s); returning original bytes", exc)
        return content


async def inject_source_stl_preview(
    *,
    sliced_3mf_bytes: bytes,
    source_library_file: LibraryFile,
    db: AsyncSession,
) -> bytes:
    """Embed the source STL's thumbnail into a preview-less sliced 3MF.

    No-ops (returns original bytes) when:

    - the source LibraryFile isn't an STL,
    - the sliced 3MF already has a preview,
    - the source file is missing on disk,
    - the on-the-fly STL render fails,
    - the 3MF rewrite throws.

    Otherwise generates / fetches the PNG, persists it on the source
    LibraryFile via ``db.flush()`` (final commit owned by the slice
    persistence flow), and returns the rewritten zip bytes — caller MUST
    re-hash.
    """
    # Lazy import: ``slice_and_persist`` calls into this module, and the
    # path / library helpers live alongside that route. Top-level import
    # would create a routes → services → routes cycle.
    from backend.app.api.routes.library import (
        get_library_thumbnails_dir,
        to_absolute_path,
        to_relative_path,
    )
    from backend.app.services.stl_thumbnail import generate_stl_thumbnail

    src_filename = (source_library_file.filename or "").lower()
    if not src_filename.endswith(".stl"):
        return sliced_3mf_bytes

    if _has_3mf_preview(sliced_3mf_bytes):
        return sliced_3mf_bytes

    # Try the source's existing thumbnail first.
    thumb_abs = to_absolute_path(source_library_file.thumbnail_path)
    if thumb_abs is None or not thumb_abs.exists():
        # Render now from the STL on disk and persist the path on the
        # source so subsequent slices (and the source's own library card)
        # reuse it.
        stl_abs = to_absolute_path(source_library_file.file_path)
        if stl_abs is None or not stl_abs.exists():
            logger.debug(
                "library_3mf_preview: source STL #%s missing on disk; skipping",
                source_library_file.id,
            )
            return sliced_3mf_bytes

        generated = generate_stl_thumbnail(stl_abs, get_library_thumbnails_dir())
        if not generated:
            logger.info(
                "library_3mf_preview: STL render failed for #%s; sliced 3MF stays preview-less",
                source_library_file.id,
            )
            return sliced_3mf_bytes

        source_library_file.thumbnail_path = to_relative_path(generated)
        await db.flush()
        thumb_abs = to_absolute_path(source_library_file.thumbnail_path)
        if thumb_abs is None or not thumb_abs.exists():
            return sliced_3mf_bytes

    try:
        png_bytes = thumb_abs.read_bytes()
    except OSError as exc:
        logger.warning("library_3mf_preview: read %s failed: %s", thumb_abs, exc)
        return sliced_3mf_bytes

    return _inject_preview(sliced_3mf_bytes, png_bytes)


__all__ = ["inject_source_stl_preview"]
