"""Custom thumbnail injector for sliced calibration 3MFs.

Bambu Studio / OrcaSlicer render three PNG thumbnails into every
sliced ``.gcode.3mf`` (``Metadata/plate_1.png`` for the primary preview,
``Metadata/top_1.png`` for the top-down view, ``Metadata/pick_1.png``
for the picker UI). For calibration prints the slicer-generated render
is just the placeholder cube — visually meaningless and confusing in
the file manager. We replace all three with a generic "PA Test" branded
image bundled under ``backend/app/data/calib_assets/thumbnails/``.

This runs as a post-slice patch (the sidecar still owns rendering for
its own bookkeeping; we overwrite the PNGs in the returned ZIP before
the bytes hit ``LibraryFile`` or the verification download). The
overwrite is mode-gated — only the calibration modes that ship a
custom thumbnail get patched; the slicer's render passes through for
the rest.
"""

from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path

from backend.app.services.calibration_constants import CaliMode

logger = logging.getLogger(__name__)

_THUMBNAILS_ROOT = Path(__file__).resolve().parent.parent / "data" / "calib_assets" / "thumbnails"

# Per-mode thumbnail map. Modes not listed pass through unchanged (the
# slicer's own render survives). All three PA modes share the same
# "PA Test" image — the comb / tower / line geometry isn't worth its
# own preview render when "PA Test" already tells the operator what the
# print is.
_MODE_TO_THUMBNAIL: dict[CaliMode, str] = {
    CaliMode.PA_TOWER: "pa_tests.png",
    CaliMode.PA_PATTERN: "pa_tests.png",
    CaliMode.PA_LINE: "pa_tests.png",
}

# Inside-zip targets we overwrite. Bambu 3MF spec, per
# ``threemf_capabilities.py`` + ``bbs_3mf.cpp``. Kept as a constant so
# callers don't have to know the layout.
_TARGET_PNGS = (
    "Metadata/plate_1.png",
    "Metadata/top_1.png",
    "Metadata/pick_1.png",
)


def apply_calibration_thumbnail(sliced_3mf_bytes: bytes, cali_mode: CaliMode) -> bytes:
    """Overwrite the slicer-generated PNG thumbnails with our per-mode image.

    Returns the original bytes unchanged when:

    - ``cali_mode`` has no entry in ``_MODE_TO_THUMBNAIL`` (no custom
      thumbnail for this mode — keep the slicer's render).
    - The thumbnail file is missing on disk (defensive — the deploy
      should ship it under ``calib_assets/thumbnails/``).
    - The input bytes aren't a valid ZIP (defensive — passes the
      sidecar's output through and lets downstream surface the real
      error).

    Logs a warning for the missing-file path so a misconfigured deploy
    surfaces in the backend log instead of silently losing branding.
    """
    thumbnail_name = _MODE_TO_THUMBNAIL.get(cali_mode)
    if thumbnail_name is None:
        return sliced_3mf_bytes

    thumbnail_path = _THUMBNAILS_ROOT / thumbnail_name
    if not thumbnail_path.exists():
        logger.warning(
            "calib_thumbnail: custom thumbnail %s missing from %s; passing slicer render through",
            thumbnail_name,
            _THUMBNAILS_ROOT,
        )
        return sliced_3mf_bytes

    try:
        thumbnail_bytes = thumbnail_path.read_bytes()
        out = io.BytesIO()
        with (
            zipfile.ZipFile(io.BytesIO(sliced_3mf_bytes), "r") as src,
            zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst,
        ):
            for name in src.namelist():
                if name in _TARGET_PNGS:
                    dst.writestr(name, thumbnail_bytes)
                else:
                    dst.writestr(name, src.read(name))
        return out.getvalue()
    except (zipfile.BadZipFile, OSError) as exc:
        logger.warning("calib_thumbnail: failed to patch thumbnails (%s); passing slicer bytes through", exc)
        return sliced_3mf_bytes


__all__ = ["apply_calibration_thumbnail"]
