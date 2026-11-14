"""Read-only, storage-independent preparation of a library file.

The caller owns persistence. A failed or changing source is never a complete
snapshot and must not replace metadata already published for a library row.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from defusedxml import ElementTree

from backend.app.services.library_helpers import SLICED_GCODE_META_KEY, detect_file_type, names_carry_sliced_gcode

EXTRACTION_VERSION = 1
EXTRACTION_KEY = "_library_extraction"
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"})
SCANNABLE_EXTENSIONS = frozenset(
    {
        ".3mf",
        ".gcode",
        ".stl",
        ".obj",
        ".step",
        ".stp",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".svg",
        ".md",
    }
)


@dataclass(frozen=True)
class PreparedLibraryFile:
    size: int
    mtime_ns: int
    digest: str
    file_type: str
    metadata: dict
    thumbnail: bytes | None = None
    thumbnail_ext: str | None = None

    def to_wire(self) -> dict:
        return {
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "digest": self.digest,
            "file_type": self.file_type,
            "metadata": self.metadata,
            "thumbnail": base64.b64encode(self.thumbnail).decode("ascii") if self.thumbnail else None,
            "thumbnail_ext": self.thumbnail_ext,
        }

    @classmethod
    def from_wire(cls, value: dict) -> PreparedLibraryFile:
        return cls(
            size=value["size"],
            mtime_ns=value["mtime_ns"],
            digest=value["digest"],
            file_type=value["file_type"],
            metadata=value["metadata"],
            thumbnail=base64.b64decode(value["thumbnail"], validate=True) if value["thumbnail"] else None,
            thumbnail_ext=value["thumbnail_ext"],
        )


def _clean(value):
    if isinstance(value, dict):
        return {
            key: _clean(item)
            for key, item in value.items()
            if not isinstance(item, bytes) and not (isinstance(key, str) and key.startswith("_"))
        }
    if isinstance(value, list):
        return [_clean(item) for item in value if not isinstance(item, bytes)]
    return value


def _gcode_thumbnail(path: Path) -> bytes | None:
    with path.open("r", errors="ignore") as stream:
        header = stream.read(50000)
    best = None
    best_width = 0
    lines: list[str] | None = None
    width = 0
    for line in header.splitlines():
        stripped = line.strip()
        if stripped.startswith("; thumbnail begin"):
            match = re.search(r"(\d+)x\d+", stripped)
            width = int(match.group(1)) if match else 0
            lines = []
        elif stripped.startswith("; thumbnail end"):
            if lines:
                try:
                    decoded = base64.b64decode("".join(lines), validate=True)
                except ValueError:
                    decoded = None
                if decoded and (best is None or best_width < width <= 300):
                    best, best_width = decoded, width
            lines = None
        elif lines is not None and stripped.startswith(";"):
            lines.append(stripped[1:].strip())
    return best


def _image_thumbnail(path: Path) -> bytes:
    from PIL import Image

    with Image.open(path) as source:
        image = source.convert("RGBA")
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.getchannel("A"))
        background.thumbnail((256, 256), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        background.save(output, "PNG", optimize=True)
        return output.getvalue()


def _assembly_set(archive: zipfile.ZipFile, names: list[str]) -> str | None:
    """The set id of a project assembled here, or None. A 3MF comes from anywhere: the record counts only beside
    the layout library's own marker and only when it parses as a record (read with its size cap)."""
    from backend.app.services.assembly import record

    if "Metadata/platenest.json" not in names or record.RECORD_MEMBER not in names:
        return None
    try:
        with archive.open(record.RECORD_MEMBER) as member:
            return record.load(member.read(record.MAX_RECORD_BYTES + 1)).set_id
    except (ValueError, OSError, zipfile.BadZipFile):
        return None


def prepare_file(path: Path, *, root: Path, display_filename: str | None = None) -> PreparedLibraryFile:
    """Prepare one file using the same parser for managed and mounted storage."""
    resolved_root = root.resolve(strict=True)
    resolved_path = path.resolve(strict=True)
    if not resolved_path.is_relative_to(resolved_root) or not resolved_path.is_file():
        raise ValueError("library source outside allowed root")
    before = resolved_path.stat()
    name = display_filename or path.name
    digest = hashlib.sha256()
    with resolved_path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    hexdigest = digest.hexdigest()
    metadata: dict = {}
    thumbnail = None
    thumbnail_ext = None
    if name.lower().endswith(".3mf"):
        # The archive parser retains its public API; compose its general and
        # per-plate results here for every library producer.
        from backend.app.services.threemf_parser_core import ThreeMFParser, parse_plates_from_3mf

        with zipfile.ZipFile(resolved_path) as archive:
            names = archive.namelist()
            # The archive parser deliberately returns partial metadata when a
            # present section fails. A library snapshot must distinguish that
            # from a legitimately absent optional section before stamping the
            # completeness marker.
            for member in names:
                if member == "Metadata/slice_info.config":
                    ElementTree.fromstring(archive.read(member))
                elif member == "Metadata/project_settings.config" or re.fullmatch(r"Metadata/plate_\d+\.json", member):
                    json.loads(archive.read(member).decode("utf-8"))
            plates = parse_plates_from_3mf(archive)
            sliced = names_carry_sliced_gcode(names)
            assembly_set = _assembly_set(archive, names)
        raw = ThreeMFParser(resolved_path).parse()
        thumbnail = raw.get("_thumbnail_data")
        thumbnail_ext = raw.get("_thumbnail_ext", ".png") if thumbnail else None
        metadata = _clean(raw)
        metadata.pop("print_name", None)
        metadata.update({"plates": plates, "is_multi_plate": len(plates) > 1, SLICED_GCODE_META_KEY: sliced})
        if assembly_set:
            metadata.update({"assembled": True, "assembly_set": assembly_set})
    elif name.lower().endswith(".gcode"):
        thumbnail = _gcode_thumbnail(resolved_path)
        thumbnail_ext = ".png" if thumbnail else None
    elif resolved_path.suffix.lower() in IMAGE_EXTENSIONS:
        thumbnail = _image_thumbnail(resolved_path)
        thumbnail_ext = ".png"
    after = resolved_path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError("library source changed during preparation")
    metadata[EXTRACTION_KEY] = {"version": EXTRACTION_VERSION, "hash": hexdigest}
    return PreparedLibraryFile(
        size=after.st_size,
        mtime_ns=after.st_mtime_ns,
        digest=hexdigest,
        file_type=detect_file_type(name),
        metadata=metadata,
        thumbnail=thumbnail,
        thumbnail_ext=thumbnail_ext,
    )


def hash_file(path: Path, *, root: Path) -> dict:
    """Check a changed stat without paying the 3MF parse cost."""
    resolved_root = root.resolve(strict=True)
    resolved_path = path.resolve(strict=True)
    if not resolved_path.is_relative_to(resolved_root) or not resolved_path.is_file():
        raise ValueError("library source outside allowed root")
    before = resolved_path.stat()
    digest = hashlib.sha256()
    with resolved_path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    after = resolved_path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError("library source changed during hash")
    return {"digest": digest.hexdigest(), "size": after.st_size, "mtime_ns": after.st_mtime_ns}
