"""A bounded look inside a sliced 3MF before it is sent (spec r2 §6, §9.1).

A preview, never the truth: the server reads the captured bytes again and decides. What this refuses, it refuses
before any upload:

- not a ZIP, a damaged member list, too many members, duplicate or unsafe member names, a declared uncompressed
  total or a per-member ratio beyond its caps (a decompression bomb);
- metadata members too large, with a broken CRC, or XML carrying a DTD or an entity declaration;
- a project without plate G-code (unsliced): nothing to print.

Nothing is extracted to disk; only metadata members are read, each under its own byte cap. G-code is never parsed.
"""

from __future__ import annotations

import re
import zipfile
import zlib
from dataclasses import dataclass, field
from xml.etree import ElementTree

MAX_MEMBERS = 10_000
MAX_TOTAL_UNCOMPRESSED = 4 * 1024 * 1024 * 1024
MAX_RATIO = 250
MAX_METADATA_BYTES = 8 * 1024 * 1024
_PLATE_GCODE = re.compile(r"^Metadata/plate_(\d{1,4})\.gcode$")
_FORBIDDEN_XML = (b"<!DOCTYPE", b"<!ENTITY")


class InspectionError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Filament:
    slot: int
    type: str
    color: str
    used_g: float


@dataclass(frozen=True)
class Plate:
    index: int
    gcode_bytes: int
    printer_model_id: str | None = None
    nozzle_diameters: str | None = None
    prediction_seconds: int | None = None
    weight_g: float | None = None
    filaments: tuple[Filament, ...] = ()


@dataclass(frozen=True)
class Inspection:
    plates: tuple[Plate, ...]
    member_count: int
    uncompressed_bytes: int
    metadata: dict = field(default_factory=dict)

    def plate(self, index: int) -> Plate | None:
        return next((p for p in self.plates if p.index == index), None)


def _safe_name(name: str) -> bool:
    return not (name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/") or ":" in name)


def _read_metadata(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    if info.file_size > MAX_METADATA_BYTES:
        raise InspectionError("metadata_too_large")
    try:
        with archive.open(info) as handle:
            data = handle.read(MAX_METADATA_BYTES + 1)
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError) as exc:  # a CRC or deflate fault surfaces here
        raise InspectionError("invalid_zip") from exc
    if len(data) > MAX_METADATA_BYTES:
        raise InspectionError("metadata_too_large")
    return data


def _parse_xml(data: bytes) -> ElementTree.Element:
    head = data[:4096].upper()
    if any(token in head for token in _FORBIDDEN_XML) or any(token in data.upper() for token in _FORBIDDEN_XML):
        raise InspectionError("xml_dtd")
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise InspectionError("invalid_metadata") from exc


def _int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _slice_info(root: ElementTree.Element) -> dict[int, dict]:
    """``Metadata/slice_info.config``: per plate, its metadata keys and filaments. A plate is named by its
    ``<metadata key="index">`` child, never by an attribute."""
    plates: dict[int, dict] = {}
    for plate in root.iter("plate"):
        meta = {m.get("key"): m.get("value") for m in plate.findall("metadata") if m.get("key")}
        index = _int(meta.get("index"))
        if index is None or index < 1:
            continue
        filaments = []
        for item in plate.findall("filament"):
            slot = _int(item.get("id"))
            if slot is None:
                continue
            filaments.append(
                Filament(
                    slot=slot,
                    type=str(item.get("type") or ""),
                    color=str(item.get("color") or ""),
                    used_g=_float(item.get("used_g")) or 0.0,
                )
            )
        plates[index] = {"meta": meta, "filaments": tuple(filaments)}
    return plates


def inspect(path: str) -> Inspection:
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise InspectionError("invalid_zip") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS:
            raise InspectionError("too_many_members")
        names: set[str] = set()
        total = 0
        for info in infos:
            if info.filename in names:
                raise InspectionError("duplicate_member")
            if not _safe_name(info.filename):
                raise InspectionError("unsafe_member")
            names.add(info.filename)
            total += info.file_size
            if (
                info.compress_size
                and info.file_size / max(info.compress_size, 1) > MAX_RATIO
                and info.file_size > 1024 * 1024
            ):
                raise InspectionError("compression_ratio")
        if total > MAX_TOTAL_UNCOMPRESSED:
            raise InspectionError("too_large_uncompressed")

        gcode = {}
        for info in infos:
            match = _PLATE_GCODE.match(info.filename)
            if match:
                gcode[int(match.group(1))] = info.file_size
        if not gcode:
            raise InspectionError("not_sliced")

        details: dict[int, dict] = {}
        slice_info = next((i for i in infos if i.filename == "Metadata/slice_info.config"), None)
        if slice_info is not None:
            details = _slice_info(_parse_xml(_read_metadata(archive, slice_info)))
        model_settings = next((i for i in infos if i.filename == "Metadata/model_settings.config"), None)
        if model_settings is not None:
            _parse_xml(_read_metadata(archive, model_settings))  # validated, not used

        plates = []
        for index in sorted(gcode):
            info = details.get(index, {})
            meta = info.get("meta", {})
            plates.append(
                Plate(
                    index=index,
                    gcode_bytes=gcode[index],
                    printer_model_id=meta.get("printer_model_id") or None,
                    nozzle_diameters=meta.get("nozzle_diameters") or None,
                    prediction_seconds=_int(meta.get("prediction")),
                    weight_g=_float(meta.get("weight")),
                    filaments=info.get("filaments", ()),
                )
            )
        return Inspection(plates=tuple(plates), member_count=len(infos), uncompressed_bytes=total)
