"""Python port of BS ``Calib.cpp::CalibPressureAdvancePattern`` (W2 Phase 2 production).

Mirrors the BambuStudio / OrcaSlicer PA Pattern wizard's runtime g-code
generator. Produces the per-layer custom-gcode entries that the slicer
then injects at the matching ``<layer top_z=...>`` boundaries inside
``Metadata/custom_gcode_per_layer.xml``. The pattern's visible geometry
— concentric box frame, K-numbered glyph tab, per-pattern V-shaped walls
at each K — are all emitted as raw ``G1`` extrusion moves, not slicer-
generated perimeters; the cube in the shipped ``pa_pattern.3mf`` scaffold
is just a placeholder that gives the slicer four ``<layer>`` boundaries.

Verification stage previously used BS's shipped XML byte-for-byte (K
range fixed at 0.0 → 0.08 step 0.005). This module replaces that path
with a faithful port so operators can pick their own ``start / end /
step``. Math constants + glyph drawing patterns + box drawing logic are
ported 1-to-1 from BS ``Calib.cpp:50-406`` (parent class primitives) and
``Calib.cpp:498-806`` (pattern orchestrator + geometry helpers).

Math validation against BS shipped scaffold (0.4mm nozzle, default
preset):
- starting_point = (45.461, 64.507) — bbox.min.x + handle_spacing offset
- frame: 84.08mm × 42.43mm, ending at (129.54, 106.94)
- tab: 84.08mm × 13.5mm, ending at (~129, 120.94)
- These match the shipped XML's first three G1 X/Y endpoints.

What is NOT ported here (still uses BS scaffold-level config):
- ``_refresh_starting_point`` model bbox lookup — the build-item
  transform in the shipped 3MF places the cube at fixed XY; we read
  that from the scaffold instead of re-deriving from model state.
- Acceleration / jerk / junction-deviation overrides — the sidecar
  inherits these from the operator's preset via ``--load-settings``.
- ``set_first_layer()`` flag — we emit unconditionally with the right
  speeds; the slicer's first-layer machinery doesn't see this custom
  gcode anyway.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Constants — verbatim from BS Calib.hpp/Calib.cpp.
M_ENCROACHMENT = 1.0 / 3.0  # CalibPressureAdvance::m_encroachment
DIGIT_SEGMENT_LEN = 2.0  # CalibPressureAdvance::m_digit_segment_len
DIGIT_GAP_LEN = 1.0  # CalibPressureAdvance::m_digit_gap_len
MAX_NUMBER_LEN = 5  # CalibPressureAdvance::m_max_number_len
HANDLE_SPACING = 2.0  # CalibPressureAdvancePattern::m_handle_spacing
NUM_LAYERS = 4  # CalibPressureAdvancePattern::m_num_layers
WALL_SIDE_LENGTH = 30.0  # CalibPressureAdvancePattern::m_wall_side_length
CORNER_ANGLE_DEG = 90  # CalibPressureAdvancePattern::m_corner_angle
PATTERN_SPACING = 2.0  # CalibPressureAdvancePattern::m_pattern_spacing
GLYPH_PADDING_HORIZONTAL = 1.0  # CalibPressureAdvancePattern::m_glyph_padding_horizontal
GLYPH_PADDING_VERTICAL = 1.0  # CalibPressureAdvancePattern::m_glyph_padding_vertical

# Travel and retraction defaults (match BS-shipped pa_pattern.3mf output).
DEFAULT_TRAVEL_SPEED_MM_MIN = 42000  # 700 mm/s — BS-shipped scaffold uses this
DEFAULT_RETRACT_LENGTH_MM = 0.8
DEFAULT_RETRACT_SPEED_MM_MIN = 1800  # 30 mm/s

# Filament defaults (typical 1.75mm with flow_ratio=1.0).
DEFAULT_FILAMENT_DIAMETER_MM = 1.75
DEFAULT_FILAMENT_FLOW_RATIO = 1.0

# Starting-point coords match BS-shipped pa_pattern.3mf. The cube in the
# scaffold is positioned via the build-item transform so its bbox is
# fixed; if we ever swap the scaffold for a re-baked one the operator
# can override these (per-mode builder passes them through).
DEFAULT_START_X = 45.461
DEFAULT_START_Y = 64.507


@dataclass(frozen=True)
class Vec2:
    x: float
    y: float

    def offset(self, dx: float, dy: float) -> Vec2:
        return Vec2(self.x + dx, self.y + dy)


def _to_radians(deg: float) -> float:
    return deg * math.pi / 180.0


def _convert_number_to_string(num: float) -> str:
    """Mirror BS ``CalibPressureAdvance::convert_number_to_string`` —
    strip trailing zeros and a dangling decimal point, e.g.
    ``0.005000`` → ``"0.005"``, ``0.0`` → ``"0"``.
    """
    s = f"{num:.6f}"
    s = s.rstrip("0").rstrip(".")
    return s if s else "0"


def _flow_mm3_per_mm(line_width: float, layer_height: float) -> float:
    """BS ``Flow::mm3_per_mm`` for non-bridge extrusion (Flow.cpp:212).

    Rectangle with semicircular ends:
        h * (w - h * (1 - PI/4))
    """
    return layer_height * (line_width - layer_height * (1.0 - math.pi / 4.0))


def _e_per_mm(line_width: float, layer_height: float, filament_diameter: float, flow_ratio: float) -> float:
    """BS ``CalibPressureAdvance::e_per_mm`` — extrusion E delta per mm
    of XY motion at the given line geometry."""
    mm3_per_mm = _flow_mm3_per_mm(line_width, layer_height)
    filament_area = math.pi * (filament_diameter / 2.0) ** 2
    return mm3_per_mm / filament_area * flow_ratio


@dataclass
class _Writer:
    """Minimal G-code writer that emits the same shape as BS
    ``GCodeWriter`` for the calls ``CalibPressureAdvance`` uses.

    Tracks ``last_pos`` so callers don't have to thread it; emits comment
    suffixes inline so the output diffs cleanly against BS's shipped
    pa_pattern.3mf scaffold.
    """

    is_bbl: bool = True
    travel_speed_mm_min: float = DEFAULT_TRAVEL_SPEED_MM_MIN
    retract_length_mm: float = DEFAULT_RETRACT_LENGTH_MM
    retract_speed_mm_min: float = DEFAULT_RETRACT_SPEED_MM_MIN
    _last_pos: Vec2 = Vec2(0.0, 0.0)
    _e_total: float = 0.0  # tracks G92 resets so caller can see cumulative E

    def retract(self) -> str:
        return f"G1 E-{self.retract_length_mm} F{self.retract_speed_mm_min:.0f}\n"

    def unretract(self) -> str:
        return f"G1 E{self.retract_length_mm} F{self.retract_speed_mm_min:.0f}\n"

    def travel_to_xy(self, pt: Vec2, comment: str = "") -> str:
        self._last_pos = pt
        line = f"G1 X{pt.x:.3f} Y{pt.y:.3f} F{self.travel_speed_mm_min:.0f}"
        if comment:
            line += f" ; {comment}"
        return line + "\n"

    def travel_to_z(self, z: float, comment: str = "") -> str:
        line = f"G1 Z{z:.3f} F{self.travel_speed_mm_min:.0f}"
        if comment:
            line += f" ; {comment}"
        return line + "\n"

    def set_speed(self, speed_mm_min: float) -> str:
        return f"G1 F{speed_mm_min:.0f}\n"

    def extrude_to_xy(self, pt: Vec2, e_delta: float, comment: str = "") -> str:
        self._last_pos = pt
        line = f"G1 X{pt.x:.3f} Y{pt.y:.3f} E{e_delta:.5f}"
        if comment:
            line += f" ; {comment}"
        return line + "\n"

    def set_pressure_advance(self, k: float) -> str:
        # GCodeWriter.cpp:354 — Bambu firmware needs L1000 M10 suffix.
        if self.is_bbl:
            return f"M900 K{k:.4f} L1000 M10 ; Override pressure advance value\n"
        return f"M900 K{k:.4f} ; Override pressure advance value\n"

    def reset_e(self) -> str:
        self._e_total = 0.0
        return "G92 E0\n"

    @property
    def last_pos(self) -> Vec2:
        return self._last_pos


def _move_to(writer: _Writer, pt: Vec2, comment: str = "") -> str:
    """BS ``CalibPressureAdvance::move_to`` — retract → travel → unretract."""
    return writer.retract() + writer.travel_to_xy(pt, comment) + writer.unretract()


def _draw_line(
    writer: _Writer,
    to_pt: Vec2,
    line_width: float,
    layer_height: float,
    speed_mm_min: float,
    filament_diameter: float,
    flow_ratio: float,
    comment: str = "",
) -> str:
    """BS ``CalibPressureAdvance::draw_line`` — set speed + extrude move
    with E computed from the geometry's mm³/mm."""
    epm = _e_per_mm(line_width, layer_height, filament_diameter, flow_ratio)
    length = math.hypot(to_pt.x - writer.last_pos.x, to_pt.y - writer.last_pos.y)
    e_delta = epm * length
    return writer.set_speed(speed_mm_min) + writer.extrude_to_xy(to_pt, e_delta, comment)


def _draw_digit(
    writer: _Writer,
    startx: float,
    starty: float,
    c: str,
    line_width: float,
    e_per_mm: float,
) -> str:
    """BS ``CalibPressureAdvance::draw_digit`` — 7-segment-like glyph
    drawing in Bottom_To_Top orientation (only mode the pattern uses).

    Layout (BS Calib.cpp:64-81):

        1-------2-------5
        |       |       |
        |       |       |
        0-------3-------4

    Numbers 0-9 trace specific point sequences. Decimal point drops a
    tiny stub from p4_5 leftward.
    """
    seg = DIGIT_SEGMENT_LEN
    gap = line_width / 2.0
    de = e_per_mm * seg
    two_de = de * 2.0

    p0 = Vec2(startx, starty)
    p0_5 = Vec2(startx, starty + seg / 2.0)
    p1 = Vec2(startx, starty + seg)
    p2 = Vec2(startx + seg, starty + seg)
    p3 = Vec2(startx + seg, starty)
    p4 = Vec2(startx + seg * 2, starty)
    p4_5 = Vec2(startx + seg * 2, starty + seg / 2.0)
    p5 = Vec2(startx + seg * 2, starty + seg)
    gap_p0_toward_p3 = p0.offset(gap, 0)
    gap_p2_toward_p3 = p2.offset(0, gap)
    dot_direction = Vec2(-seg / 2.0, 0)

    out = ""
    if c == "0":
        out += _move_to(writer, p0, "Glyph: 0")
        out += writer.extrude_to_xy(p1, de)
        out += writer.extrude_to_xy(p5, two_de)
        out += writer.extrude_to_xy(p4, de)
        out += writer.extrude_to_xy(gap_p0_toward_p3, two_de)
    elif c == "1":
        out += _move_to(writer, p0_5, "Glyph: 1")
        out += writer.extrude_to_xy(p4_5, two_de)
    elif c == "2":
        out += _move_to(writer, p0, "Glyph: 2")
        out += writer.extrude_to_xy(p1, de)
        out += writer.extrude_to_xy(p2, de)
        out += writer.extrude_to_xy(p3, de)
        out += writer.extrude_to_xy(p4, de)
        out += writer.extrude_to_xy(p5, de)
    elif c == "3":
        out += _move_to(writer, p0, "Glyph: 3")
        out += writer.extrude_to_xy(p1, de)
        out += writer.extrude_to_xy(p5, two_de)
        out += writer.extrude_to_xy(p4, de)
        out += _move_to(writer, gap_p2_toward_p3)
        out += writer.extrude_to_xy(p3, de)
    elif c == "4":
        out += _move_to(writer, p0, "Glyph: 4")
        out += writer.extrude_to_xy(p3, de)
        out += writer.extrude_to_xy(p2, de)
        out += _move_to(writer, p1)
        out += writer.extrude_to_xy(p5, two_de)
    elif c == "5":
        out += _move_to(writer, p1, "Glyph: 5")
        out += writer.extrude_to_xy(p0, de)
        out += writer.extrude_to_xy(p3, de)
        out += writer.extrude_to_xy(p2, de)
        out += writer.extrude_to_xy(p5, de)
        out += writer.extrude_to_xy(p4, de)
    elif c == "6":
        out += _move_to(writer, p1, "Glyph: 6")
        out += writer.extrude_to_xy(p0, de)
        out += writer.extrude_to_xy(p4, two_de)
        out += writer.extrude_to_xy(p5, de)
        out += writer.extrude_to_xy(p2, de)
        out += writer.extrude_to_xy(p3, de)
    elif c == "7":
        out += _move_to(writer, p0, "Glyph: 7")
        out += writer.extrude_to_xy(p1, de)
        out += writer.extrude_to_xy(p5, two_de)
    elif c == "8":
        out += _move_to(writer, p2, "Glyph: 8")
        out += writer.extrude_to_xy(p3, de)
        out += writer.extrude_to_xy(p4, de)
        out += writer.extrude_to_xy(p5, de)
        out += writer.extrude_to_xy(p1, two_de)
        out += writer.extrude_to_xy(p0, de)
        out += writer.extrude_to_xy(p3, de)
    elif c == "9":
        out += _move_to(writer, p5, "Glyph: 9")
        out += writer.extrude_to_xy(p1, two_de)
        out += writer.extrude_to_xy(p0, de)
        out += writer.extrude_to_xy(p3, de)
        out += writer.extrude_to_xy(p2, de)
    elif c == ".":
        out += _move_to(writer, p4_5, "Glyph: .")
        out += writer.extrude_to_xy(p4_5.offset(dot_direction.x, dot_direction.y), de)
    # else: unknown glyph — skip silently, matching BS default branch.
    return out


def _number_spacing() -> float:
    return DIGIT_SEGMENT_LEN + DIGIT_GAP_LEN


def _draw_number(
    writer: _Writer,
    startx: float,
    starty: float,
    value: float,
    line_width: float,
    e_per_mm: float,
    speed_mm_min: float,
) -> str:
    """BS ``CalibPressureAdvance::draw_number`` in Bottom_To_Top mode —
    stack each digit vertically by ``number_spacing``."""
    s_number = _convert_number_to_string(value)
    out = writer.set_speed(speed_mm_min)
    for i, ch in enumerate(s_number):
        if i > MAX_NUMBER_LEN:
            break
        out += _draw_digit(writer, startx, starty + i * _number_spacing(), ch, line_width, e_per_mm)
    return out


@dataclass(frozen=True)
class _DrawBoxOpts:
    is_filled: bool
    num_perimeters: int
    height: float
    line_width: float
    speed_mm_min: float


def _draw_box(
    writer: _Writer,
    min_x: float,
    min_y: float,
    size_x: float,
    size_y: float,
    opts: _DrawBoxOpts,
    filament_diameter: float,
    flow_ratio: float,
) -> str:
    """BS ``CalibPressureAdvance::draw_box`` (Calib.cpp:226-406).

    Concentric-perimeter outline + optional 45° hatched fill. Cap
    ``num_perimeters`` at the maximum the box's smaller dim can hold
    (BS clamps explicitly before laying perimeters). The fill is
    written as one continuous zigzag of diagonal lines; the
    boundary-walk logic mirrors BS's 3-branch case-split (i < min(x,y),
    i < max(x,y), else) and the x_remainder / y_remainder corner cases.
    """
    out = ""
    max_x = min_x + size_x
    max_y = min_y + size_y
    spacing = opts.line_width - opts.height * (1.0 - math.pi / 4.0)

    # Clamp perimeters to max that fits inside the box at 45° angle.
    sin45 = math.sin(_to_radians(45))
    max_perim = min(
        math.floor((size_x * sin45) / (spacing / sin45)),
        math.floor((size_y * sin45) / (spacing / sin45)),
    )
    num_perim = min(opts.num_perimeters, int(max_perim))

    out += _move_to(writer, Vec2(min_x, min_y), "Move to box start")

    x, y = min_x, min_y
    for i in range(num_perim):
        if i != 0:
            x += spacing
            y += spacing
            out += _move_to(writer, Vec2(x, y), "Step inwards to print next perimeter")
        # walk up
        y += size_y - i * spacing * 2
        out += _draw_line(
            writer,
            Vec2(x, y),
            opts.line_width,
            opts.height,
            opts.speed_mm_min,
            filament_diameter,
            flow_ratio,
            "Draw perimeter (up)",
        )
        # walk right
        x += size_x - i * spacing * 2
        out += _draw_line(
            writer,
            Vec2(x, y),
            opts.line_width,
            opts.height,
            opts.speed_mm_min,
            filament_diameter,
            flow_ratio,
            "Draw perimeter (right)",
        )
        # walk down
        y -= size_y - i * spacing * 2
        out += _draw_line(
            writer,
            Vec2(x, y),
            opts.line_width,
            opts.height,
            opts.speed_mm_min,
            filament_diameter,
            flow_ratio,
            "Draw perimeter (down)",
        )
        # walk left
        x -= size_x - i * spacing * 2
        out += _draw_line(
            writer,
            Vec2(x, y),
            opts.line_width,
            opts.height,
            opts.speed_mm_min,
            filament_diameter,
            flow_ratio,
            "Draw perimeter (left)",
        )

    if not opts.is_filled:
        return out

    spacing_45 = spacing / sin45
    bound_modifier = (spacing * (num_perim - 1)) + (opts.line_width * (1.0 - M_ENCROACHMENT))
    x_min_b = min_x + bound_modifier
    x_max_b = max_x - bound_modifier
    y_min_b = min_y + bound_modifier
    y_max_b = max_y - bound_modifier
    x_count = int(math.floor((x_max_b - x_min_b) / spacing_45))
    y_count = int(math.floor((y_max_b - y_min_b) / spacing_45))
    x_remainder = (x_max_b - x_min_b) % spacing_45
    y_remainder = (y_max_b - y_min_b) % spacing_45

    x, y = x_min_b, y_min_b
    out += _move_to(writer, Vec2(x, y), "Move to fill start")

    extra_iter = 1 if (x_remainder + y_remainder >= spacing_45) else 0
    total_iters = x_count + y_count + extra_iter

    def _line(to_x: float, to_y: float, comment: str) -> str:
        return _draw_line(
            writer,
            Vec2(to_x, to_y),
            opts.line_width,
            opts.height,
            opts.speed_mm_min,
            filament_diameter,
            flow_ratio,
            comment,
        )

    for i in range(total_iters):
        if i < min(x_count, y_count):
            if i % 2 == 0:
                x += spacing_45
                y = y_min_b
                out += _move_to(writer, Vec2(x, y), "Fill: Step right")
                y += x - x_min_b
                x = x_min_b
                out += _line(x, y, "Fill: Print up/left")
            else:
                y += spacing_45
                x = x_min_b
                out += _move_to(writer, Vec2(x, y), "Fill: Step up")
                x += y - y_min_b
                y = y_min_b
                out += _line(x, y, "Fill: Print down/right")
        elif i < max(x_count, y_count):
            if x_count > y_count:
                # box wider than tall
                if i % 2 == 0:
                    x += spacing_45
                    y = y_min_b
                    out += _move_to(writer, Vec2(x, y), "Fill: Step right")
                    x -= y_max_b - y_min_b
                    y = y_max_b
                    out += _line(x, y, "Fill: Print up/left")
                else:
                    if i == y_count:
                        x += spacing_45 - y_remainder
                        y_remainder = 0.0
                    else:
                        x += spacing_45
                    y = y_max_b
                    out += _move_to(writer, Vec2(x, y), "Fill: Step right")
                    x += y_max_b - y_min_b
                    y = y_min_b
                    out += _line(x, y, "Fill: Print down/right")
            else:
                # box taller than wide
                if i % 2 == 0:
                    x = x_max_b
                    if i == x_count:
                        y += spacing_45 - x_remainder
                        x_remainder = 0.0
                    else:
                        y += spacing_45
                    out += _move_to(writer, Vec2(x, y), "Fill: Step up")
                    x = x_min_b
                    y += x_max_b - x_min_b
                    out += _line(x, y, "Fill: Print up/left")
                else:
                    x = x_min_b
                    y += spacing_45
                    out += _move_to(writer, Vec2(x, y), "Fill: Step up")
                    x = x_max_b
                    y -= x_max_b - x_min_b
                    out += _line(x, y, "Fill: Print down/right")
        else:
            if i % 2 == 0:
                x = x_max_b
                if i == x_count:
                    y += spacing_45 - x_remainder
                else:
                    y += spacing_45
                out += _move_to(writer, Vec2(x, y), "Fill: Step up")
                x -= y_max_b - y
                y = y_max_b
                out += _line(x, y, "Fill: Print up/left")
            else:
                if i == y_count:
                    x += spacing_45 - y_remainder
                else:
                    x += spacing_45
                y = y_max_b
                out += _move_to(writer, Vec2(x, y), "Fill: Step right")
                y -= x_max_b - x
                x = x_max_b
                out += _line(x, y, "Fill: Print down/right")
    return out


@dataclass(frozen=True)
class PAPatternParams:
    """Inputs to the pattern generator. Mirrors BS ``Calib_Params`` +
    config knobs that the C++ class would read from
    ``DynamicPrintConfig``."""

    start_pa: float
    end_pa: float
    step_pa: float
    nozzle_diameter: float = 0.4
    layer_height: float = 0.2
    initial_layer_height: float = 0.25
    line_width: float = 0.0  # 0 → derive nozzle * 1.125
    initial_layer_line_width: float = 0.0  # 0 → derive nozzle * 1.4
    wall_count: int = 3
    speed_first_layer_mm_s: float = 30.0
    speed_perimeter_mm_s: float = 100.0
    filament_diameter: float = DEFAULT_FILAMENT_DIAMETER_MM
    filament_flow_ratio: float = DEFAULT_FILAMENT_FLOW_RATIO
    start_x: float = DEFAULT_START_X
    start_y: float = DEFAULT_START_Y
    is_bbl: bool = True


@dataclass(frozen=True)
class PatternLayer:
    """One output entry — what the per-layer ``<layer extra=...>``
    attribute should hold, plus the ``top_z`` it triggers at."""

    print_z: float
    extra: str


def _resolved_line_width(p: PAPatternParams) -> float:
    return p.line_width if p.line_width > 0 else p.nozzle_diameter * 1.125


def _resolved_initial_line_width(p: PAPatternParams) -> float:
    return p.initial_layer_line_width if p.initial_layer_line_width > 0 else p.nozzle_diameter * 1.4


def _line_spacing(p: PAPatternParams) -> float:
    return _resolved_line_width(p) - p.layer_height * (1.0 - math.pi / 4.0)


def _line_spacing_first_layer(p: PAPatternParams) -> float:
    return _resolved_initial_line_width(p) - p.initial_layer_height * (1.0 - math.pi / 4.0)


def _line_spacing_angle(p: PAPatternParams) -> float:
    return _line_spacing(p) / math.sin(_to_radians(CORNER_ANGLE_DEG / 2.0))


def _num_patterns(p: PAPatternParams) -> int:
    return int(math.ceil((p.end_pa - p.start_pa) / p.step_pa + 1))


def _pattern_shift(p: PAPatternParams) -> float:
    return (
        (p.wall_count - 1) * _line_spacing_first_layer(p) + _resolved_initial_line_width(p) + GLYPH_PADDING_HORIZONTAL
    )


def _frame_size_y(_p: PAPatternParams) -> float:
    return math.sin(_to_radians(CORNER_ANGLE_DEG / 2.0)) * WALL_SIDE_LENGTH * 2


def _glyph_length_x(p: PAPatternParams) -> float:
    return _resolved_line_width(p) + 2 * DIGIT_SEGMENT_LEN


def _glyph_start_x(p: PAPatternParams, pattern_i: int) -> float:
    x = (
        p.start_x
        + _pattern_shift(p)
        + pattern_i * (p.wall_count - 1) * _line_spacing_angle(p)
        + pattern_i * _resolved_line_width(p)
        + pattern_i * PATTERN_SPACING
    )
    x += p.wall_count * _line_spacing_angle(p) / 2.0
    x -= _glyph_length_x(p) / 2.0
    return x


def _glyph_tab_max_x(p: PAPatternParams) -> float:
    num = _num_patterns(p)
    max_num = (num - 1) if (num % 2 == 0) else num
    padding = _glyph_start_x(p, 0) - p.start_x
    return _glyph_start_x(p, max_num - 1) + (_glyph_length_x(p) - _resolved_line_width(p) / 2.0) + padding


def _max_numbering_height(p: PAPatternParams) -> float:
    """Longest K-string character count × digit_segment_len + gaps."""
    most_chars = 0
    n = _num_patterns(p)
    # Only every other glyph is printed (matches BS — line numbering at
    # alternating patterns to avoid overlap).
    for i in range(0, n, 2):
        s = _convert_number_to_string(p.start_pa + i * p.step_pa)
        if len(s) > most_chars:
            most_chars = len(s)
    most_chars = min(most_chars, MAX_NUMBER_LEN)
    return most_chars * DIGIT_SEGMENT_LEN + (most_chars - 1) * DIGIT_GAP_LEN


def _object_size_x(p: PAPatternParams) -> float:
    n = _num_patterns(p)
    return (
        n * ((p.wall_count - 1) * _line_spacing_angle(p))
        + (n - 1) * (PATTERN_SPACING + _resolved_line_width(p))
        + math.cos(_to_radians(CORNER_ANGLE_DEG / 2.0)) * WALL_SIDE_LENGTH
        + _line_spacing_first_layer(p) * p.wall_count
    )


def _max_layer_z(p: PAPatternParams) -> float:
    return p.initial_layer_height + (NUM_LAYERS - 1) * p.layer_height


def _speed_adjust(speed_mm_s: float) -> float:
    return speed_mm_s * 60.0  # mm/s → mm/min for G1 F


def generate_pa_pattern_layers(params: PAPatternParams) -> list[PatternLayer]:
    """Generate the four per-layer custom-gcode strings + their
    ``top_z`` boundaries.

    Mirrors BS ``CalibPressureAdvancePattern::generate_custom_gcodes``
    (Calib.cpp:506-656). Returns four entries — one per print layer —
    that the caller (calib_pa_pattern.py builder) packs into the 3MF's
    ``Metadata/custom_gcode_per_layer.xml`` as
    ``<layer top_z=...> extra=...``.

    Layer split (matching BS):

    - Layer 0 (i=0): anchor frame + glyph tab + first row of pattern V's at PA=start.
      Stored entry ``print_z = initial_layer_height``.
    - Layer 1 (i=1): K-value glyphs on every other pattern + V's at incremented PA.
      Stored entry ``print_z = initial_layer_height + 0 * layer_height``.
    - Layer 2 (i=2): V's only at next PA increment.
      Stored entry ``print_z = initial_layer_height + 1 * layer_height``.
    - Layer 3 (i=3): final V's. Final entry ``print_z = max_layer_z``.
    """
    p = params
    line_width = _resolved_line_width(p)
    init_line_width = _resolved_initial_line_width(p)
    layer_h = p.layer_height
    init_h = p.initial_layer_height
    num_pat = _num_patterns(p)
    wall_count = p.wall_count
    frame_y = _frame_size_y(p)
    print_x = _object_size_x(p) + _pattern_shift(p)

    e_per_mm_layer = _e_per_mm(line_width, layer_h, p.filament_diameter, p.filament_flow_ratio)
    speed_first_layer_mm_min = _speed_adjust(p.speed_first_layer_mm_s)
    speed_perimeter_mm_min = _speed_adjust(p.speed_perimeter_mm_s)

    starting_point = Vec2(p.start_x, p.start_y)

    writer = _Writer(is_bbl=p.is_bbl)

    gcode = "; start pressure advance pattern for layer\n"

    # Initial XY + Z + PA setup (layer 0 prologue).
    gcode += _move_to(writer, starting_point, "Move to start XY position")
    gcode += writer.travel_to_z(init_h, "Move to start Z position")
    gcode += writer.set_pressure_advance(p.start_pa)

    default_box_opts = _DrawBoxOpts(
        is_filled=False,
        num_perimeters=wall_count,
        height=init_h,
        line_width=init_line_width,
        speed_mm_min=speed_first_layer_mm_min,
    )

    # Anchor frame.
    gcode += _draw_box(
        writer,
        starting_point.x,
        starting_point.y,
        print_x,
        frame_y,
        default_box_opts,
        p.filament_diameter,
        p.filament_flow_ratio,
    )

    # Glyph tab (filled).
    tab_opts = _DrawBoxOpts(
        is_filled=True,
        num_perimeters=wall_count,
        height=init_h,
        line_width=init_line_width,
        speed_mm_min=speed_first_layer_mm_min,
    )
    gcode += _draw_box(
        writer,
        starting_point.x,
        starting_point.y + frame_y + _line_spacing_first_layer(p),
        _glyph_tab_max_x(p) - starting_point.x,
        _max_numbering_height(p) + _line_spacing_first_layer(p) + GLYPH_PADDING_VERTICAL * 2,
        tab_opts,
        p.filament_diameter,
        p.filament_flow_ratio,
    )

    layers_out: list[PatternLayer] = []

    for i in range(NUM_LAYERS):
        layer_height_z = init_h + (i * layer_h)
        zhop_height = layer_height_z + layer_h

        if i > 0:
            gcode += "; end pressure advance pattern for layer\n"
            layers_out.append(
                PatternLayer(
                    print_z=init_h + (i - 1) * layer_h,
                    extra=gcode,
                )
            )
            gcode = "; start pressure advance pattern for layer\n"
            gcode += writer.travel_to_z(layer_height_z, "Move to layer height")
            gcode += writer.reset_e()

        # Glyph row on i=1 only — every other pattern.
        if i == 1:
            gcode += writer.set_pressure_advance(p.start_pa)
            number_e_per_mm = e_per_mm_layer
            for j in range(0, num_pat, 2):
                gcode += _draw_number(
                    writer,
                    _glyph_start_x(p, j),
                    starting_point.y + frame_y + GLYPH_PADDING_VERTICAL + line_width,
                    p.start_pa + j * p.step_pa,
                    line_width,
                    number_e_per_mm,
                    speed_first_layer_mm_min,
                )

        if i == 0:
            gcode += writer.set_pressure_advance(p.start_pa)

        to_x = starting_point.x + _pattern_shift(p)
        to_y = starting_point.y
        side_length = WALL_SIDE_LENGTH

        # Shrink layer 0 to fit inside the frame.
        if i == 0:
            shrink = (
                _line_spacing_first_layer(p) * (wall_count - 1) + (init_line_width * (1.0 - M_ENCROACHMENT))
            ) / math.sin(_to_radians(CORNER_ANGLE_DEG) / 2.0)
            side_length = WALL_SIDE_LENGTH - shrink
            to_x += shrink * math.sin(_to_radians(90) - _to_radians(CORNER_ANGLE_DEG) / 2.0)
            to_y += _line_spacing_first_layer(p) * (wall_count - 1) + (init_line_width * (1.0 - M_ENCROACHMENT))

        initial_x = to_x
        initial_y = to_y

        gcode += writer.travel_to_z(zhop_height, "z-hop before move")
        gcode += _move_to(writer, Vec2(to_x, to_y), "Move to pattern start")
        gcode += writer.travel_to_z(layer_height_z, "undo z-hop")

        cos_half = math.cos(_to_radians(CORNER_ANGLE_DEG) / 2.0)
        sin_half = math.sin(_to_radians(CORNER_ANGLE_DEG) / 2.0)
        draw_line_height = init_h if i == 0 else layer_h
        draw_line_speed = speed_first_layer_mm_min if i == 0 else speed_perimeter_mm_min

        for j in range(num_pat):
            gcode += writer.set_pressure_advance(p.start_pa + j * p.step_pa)
            for k in range(wall_count):
                to_x += cos_half * side_length
                to_y += sin_half * side_length
                gcode += _draw_line(
                    writer,
                    Vec2(to_x, to_y),
                    line_width,
                    draw_line_height,
                    draw_line_speed,
                    p.filament_diameter,
                    p.filament_flow_ratio,
                    "Print pattern wall",
                )
                to_x -= cos_half * side_length
                to_y += sin_half * side_length
                gcode += _draw_line(
                    writer,
                    Vec2(to_x, to_y),
                    line_width,
                    draw_line_height,
                    draw_line_speed,
                    p.filament_diameter,
                    p.filament_flow_ratio,
                    "Print pattern wall",
                )
                to_y = initial_y
                if k != wall_count - 1:
                    to_x += _line_spacing_angle(p)
                    gcode += writer.travel_to_z(zhop_height, "z-hop before move")
                    gcode += _move_to(writer, Vec2(to_x, to_y), "Move to start next pattern wall")
                    gcode += writer.travel_to_z(layer_height_z, "undo z-hop")
                elif j != num_pat - 1:
                    to_x += PATTERN_SPACING + line_width
                    gcode += writer.travel_to_z(zhop_height, "z-hop before move")
                    gcode += _move_to(writer, Vec2(to_x, to_y), "Move to next pattern")
                    gcode += writer.travel_to_z(layer_height_z, "undo z-hop")
                elif i != NUM_LAYERS - 1:
                    to_x = initial_x
                    gcode += writer.travel_to_z(zhop_height, "z-hop before move")
                    gcode += _move_to(writer, Vec2(to_x, to_y), "Move back to start position")
                    gcode += writer.travel_to_z(layer_height_z, "undo z-hop")
                    gcode += writer.reset_e()
                # else: everything done

    gcode += writer.set_pressure_advance(p.start_pa)
    gcode += "; end pressure advance pattern for layer\n"
    layers_out.append(PatternLayer(print_z=_max_layer_z(p), extra=gcode))

    return layers_out
