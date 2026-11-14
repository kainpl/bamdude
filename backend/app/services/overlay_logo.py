"""The installation's logo on the stream overlay — one file, one writer (G09; upstream 67d3e7e86).

``DATA_DIR/overlay-branding/logo.png`` is the whole store: one logo per installation, carried by backups
(``backup_files.directories``). Only this module writes or removes it.

A logo is never stored as sent. It is composited into a public broadcast, so what is kept is its pixels and
nothing else: decoded by the PNG / JPEG / WebP decoders alone (an SVG is script and is refused with everything
else), turned upright by its EXIF orientation, scaled to :data:`MAX_SIDE` and saved again as a PNG with no
metadata — no EXIF (GPS, the camera's serial), no XMP, no text chunks, no colour profile.

The revision is the SHA-256 of the stored bytes, so a client can cache one revision for good. It is read off
the file, not remembered for the life of the process: a restore replaces the directory behind this module.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import tempfile
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from backend.app.core.config import settings
from backend.app.services import preview_artifacts

logger = logging.getLogger(__name__)

DIRECTORY_NAME = "overlay-branding"
FILE_NAME = "logo.png"
MEDIA_TYPE = "image/png"
#: What an upload may weigh, and how many pixels it may decode to.
MAX_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 4_000_000
#: The longer side of the stored logo. An overlay draws it a few dozen pixels high on a 1080p canvas.
MAX_SIDE = 512
#: How long an answer about the file is reused. The overlay feed asks every two seconds per open overlay.
CACHE_SECONDS = 5.0
# The decoders an upload is opened with, so no other Pillow plugin ever parses its bytes. A JPEG with a second
# image (``MPO``) is answered by the JPEG decoder.
_DECODERS = ("PNG", "JPEG", "WEBP")
_ACCEPTED = frozenset({"PNG", "JPEG", "MPO", "WEBP"})


class LogoRefused(Exception):
    """An upload that is not a logo. ``reason``: ``too_large`` / ``too_many_pixels`` / ``animated`` / ``not_an_image``."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Logo:
    revision: str
    content: bytes


def directory() -> Path:
    return Path(settings.base_dir) / DIRECTORY_NAME


def _path() -> Path:
    return directory() / FILE_NAME


# (when it was read, the file's stat signature, the logo) — the logo is None when there is no file.
_cache: tuple[float, tuple[int, int] | None, Logo | None] | None = None


def forget() -> None:
    """Drop the remembered answer: the next question reads the file."""
    global _cache
    _cache = None


def _read() -> tuple[tuple[int, int] | None, Logo | None]:
    path = _path()
    try:
        info = path.stat()
        content = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None, None
    return (info.st_mtime_ns, info.st_size), Logo(hashlib.sha256(content).hexdigest()[:32], content)


def _signature() -> tuple[int, int] | None:
    try:
        info = _path().stat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    return info.st_mtime_ns, info.st_size


async def current() -> Logo | None:
    """The saved logo, or None. Off the event loop; re-read when the file changed."""
    global _cache
    cached = _cache
    now = time.monotonic()
    if cached is not None and now - cached[0] < CACHE_SECONDS:
        return cached[2]
    signature = await preview_artifacts.disk(_signature)
    if cached is not None and signature == cached[1]:
        _cache = (now, cached[1], cached[2])
        return cached[2]
    signature, logo = await preview_artifacts.disk(_read)
    _cache = (now, signature, logo)
    return logo


def _encode(content: bytes) -> bytes:
    """The upload as the PNG that is stored, or :class:`LogoRefused`."""
    if len(content) > MAX_BYTES:
        raise LogoRefused("too_large")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content), formats=_DECODERS) as image:
                if image.format not in _ACCEPTED:
                    raise LogoRefused("not_an_image")
                if image.width * image.height > MAX_PIXELS:
                    raise LogoRefused("too_many_pixels")
                if image.format != "MPO" and getattr(image, "is_animated", False):
                    raise LogoRefused("animated")
                image.load()  # a file cut short is refused before anything is written
                upright = ImageOps.exif_transpose(image)
                upright = upright.convert("RGBA")
                upright.thumbnail((MAX_SIDE, MAX_SIDE))
                # A new image holds the pixels and nothing the upload carried beside them.
                clean = Image.new("RGBA", upright.size)
                clean.paste(upright)
                buffer = io.BytesIO()
                clean.save(buffer, "PNG", optimize=True)
                return buffer.getvalue()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise LogoRefused("too_many_pixels") from exc
    except (OSError, ValueError, SyntaxError, EOFError) as exc:
        raise LogoRefused("not_an_image") from exc


def _write(content: bytes) -> None:
    png = _encode(content)
    target = directory()
    target.mkdir(parents=True, exist_ok=True)
    # Replaced atomically, so a reader never sees half a PNG; the temporary file never outlives this call.
    handle, temporary = tempfile.mkstemp(dir=target, prefix=".logo-", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(png)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target / FILE_NAME)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _remove() -> None:
    _path().unlink(missing_ok=True)


async def save(content: bytes) -> Logo:
    """Validate and store a logo, replacing the saved one; a refused upload leaves the saved one as it was.

    The file work runs in a thread a cancelled caller still waits for (``preview_artifacts.disk``).
    """
    try:
        await preview_artifacts.disk(_write, content)
    finally:
        forget()
    logo = await current()
    if logo is None:  # removed again between the write and the read
        raise LogoRefused("not_an_image")
    logger.info("Stream overlay logo saved (revision %s, %d bytes)", logo.revision, len(logo.content))
    return logo


async def remove() -> None:
    try:
        await preview_artifacts.disk(_remove)
    finally:
        forget()
    logger.info("Stream overlay logo removed")
