"""Where parts may be put: the work zone of one nozzle, read from a flattened printer preset.

The plate size is not the work zone. A Bambu printer preset says, in six keys, where a nozzle reaches and
what must stay clear: ``printable_area`` and ``printable_height``; per extruder ``extruder_printable_area``
and ``extruder_printable_height`` (entry ``i`` belongs to extruder ``i + 1`` — Bambu Studio reads ``[0]`` as
the left nozzle and ``[1]`` as the right one); ``bed_exclude_area`` and ``wrapping_exclude_area``.

The slicer's command line does not check a part against a nozzle's reach, so keeping parts inside it is ours
to do. ``wrapping_exclude_area`` only matters to the slicer when wrap detection is on; it is excluded here
always — it is small, and a part printed into it costs a ruined print.

Pure functions; no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from platenest import Zone

Point = tuple[float, float]


class GeometryUnknown(Exception):
    """The preset does not say where the printer can print."""


@dataclass(frozen=True)
class NozzleInfo:
    index: int  # 1-based extruder number, as the slicer's ``filament_map`` counts
    area: tuple[float, float, float, float]  # x0, y0, x1, y1 of its reach
    max_height: float


@dataclass(frozen=True)
class PrinterGeometry:
    bed_size: tuple[float, float]  # the far corner of printable_area
    nozzles: tuple[NozzleInfo, ...]  # one entry for a single-nozzle printer
    excluded: tuple[tuple[Point, ...], ...]


def _point(text: object) -> Point:
    x, y = str(text).strip().lower().split("x")
    return float(x), float(y)


def _points(value: object) -> list[Point]:
    """A point list written either as a list of "XxY" or as one "XxY,XxY,…" string."""
    if isinstance(value, str):
        value = [part for part in value.split(",") if part.strip()]
    if not isinstance(value, (list, tuple)):
        raise ValueError("not a point list")
    return [_point(item) for item in value]


def _bounds(points: list[Point]) -> tuple[float, float, float, float]:
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _number(value: object) -> float:
    if isinstance(value, (list, tuple)):
        value = value[0]
    return float(str(value).strip())


def _polygons(value: object) -> list[tuple[Point, ...]]:
    """Excluded areas. The common profile writes two rectangles as ONE eight-point list; split such a list."""
    if not value:
        return []
    try:
        points = _points(value)
    except (ValueError, TypeError):
        return []
    if len(points) > 4 and len(points) % 4 == 0:
        return [tuple(points[i : i + 4]) for i in range(0, len(points), 4)]
    return [tuple(points)] if len(points) >= 3 else []


def printer_geometry(flat: dict) -> PrinterGeometry:
    """Read the geometry keys of a flattened printer preset. :class:`GeometryUnknown` when they are unusable."""
    try:
        bed = _points(flat.get("printable_area"))
        if len(bed) < 3:
            raise ValueError("printable_area needs at least three points")
        x0, y0, x1, y1 = _bounds(bed)
        if x1 <= x0 or y1 <= y0:
            raise ValueError("printable_area has no extent")
        height = _number(flat["printable_height"])
        if height <= 0:
            raise ValueError("printable_height must be positive")
    except (KeyError, ValueError, TypeError, IndexError) as error:
        raise GeometryUnknown(f"the printer preset does not describe its print area ({error})") from None

    areas: list[tuple[float, float, float, float]] = []
    for entry in flat.get("extruder_printable_area") or []:
        try:
            points = _points(entry)
        except (ValueError, TypeError):
            raise GeometryUnknown("the printer preset has an unreadable nozzle area") from None
        if len(points) >= 3:
            areas.append(_bounds(points))
    if not areas:
        areas = [(x0, y0, x1, y1)]

    heights = flat.get("extruder_printable_height") or []
    nozzles = []
    for number, area in enumerate(areas, start=1):
        try:
            nozzle_height = _number(heights[number - 1]) if number <= len(heights) else height
        except (ValueError, TypeError):
            nozzle_height = height
        nozzles.append(NozzleInfo(number, area, nozzle_height if nozzle_height > 0 else height))

    excluded = _polygons(flat.get("bed_exclude_area")) + _polygons(flat.get("wrapping_exclude_area"))
    return PrinterGeometry((x1, y1), tuple(nozzles), tuple(excluded))


def work_zone(geometry: PrinterGeometry, nozzle: int) -> Zone:
    """The zone for parts printed with extruder ``nozzle`` (1-based). ``ValueError`` when there is no such one."""
    chosen = next((n for n in geometry.nozzles if n.index == nozzle), None)
    if chosen is None:
        raise ValueError(f"the printer has no nozzle {nozzle}")
    x0, y0, x1, y1 = chosen.area
    return Zone(
        width=geometry.bed_size[0],
        depth=geometry.bed_size[1],
        allowed=[((x0, y0), (x1, y0), (x1, y1), (x0, y1))],
        excluded=list(geometry.excluded),
        max_height=chosen.max_height,
    )
