"""Cached PIL font handles for the label raster.

Separate from ``label_raster`` so the cache is not rebuilt per render, and
separate from ``label_renderer`` because reportlab and PIL want the same files
through completely different objects. The files themselves, and why they are
vendored rather than taken from the system, are documented in
``backend/app/data/fonts/README.md``.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

logger = logging.getLogger(__name__)

_FONT_DIR = Path(__file__).resolve().parent.parent / "data" / "fonts"
_FILES: dict[tuple[bool, bool], Path] = {
    (False, False): _FONT_DIR / "Arimo-Regular.ttf",
    (True, False): _FONT_DIR / "Arimo-Bold.ttf",
    (False, True): _FONT_DIR / "Arimo-Italic.ttf",
}


@lru_cache(maxsize=128)
def font_at(size: int, *, bold: bool = False, italic: bool = False) -> ImageFont.FreeTypeFont:
    """A face at one pixel size, loaded once.

    Falls back to PIL's built-in bitmap font when the files are missing. That is
    a bad label rather than no label — the same trade ``label_renderer`` makes,
    for the same reason: a missing font file should not fail an inventory
    action. It is loud in the log because a silent fallback here re-creates
    exactly the Cyrillic bug the vendored fonts exist to fix.
    """
    path = _FILES.get((bold, italic), _FILES[(False, False)])
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        logger.error(
            "Label raster font %s could not be loaded; falling back to PIL's default. "
            "Non-Latin text will not render correctly.",
            path,
        )
        return ImageFont.load_default()


__all__ = ["font_at"]
