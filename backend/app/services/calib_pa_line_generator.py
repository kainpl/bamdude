"""Python port of BS ``Calib.cpp::CalibPressureAdvanceLine`` (W2 Phase 9).

Mirrors the BambuStudio / OrcaSlicer PA Line wizard's runtime g-code
generator. Produces a single-layer block of g-code: a prime line + N
horizontal rows of three-segment extrusions (slow / fast / slow) with
the printer's pressure-advance K stepped per row, plus an optional
filled glyph box on the right with K-value labels next to every other
row.

The shipped ``pressure_advance_test.stl`` is a low-poly 68×14×0.2 mm
placeholder — BS engine bypasses slicing it entirely when
``calib_mode == Calib_PA_Line`` and writes ``print_pa_lines()`` output
in its place. Path C can't reach that engine branch through the sidecar
CLI, so we emit equivalent g-code as a single ``<layer top_z=0.2>``
``custom_gcode_per_layer.xml`` entry against a 1-layer cube placeholder
and let the slicer treat it as a custom-gcode injection (mirrors how
PA Pattern's pre-baked layers ride on the cube scaffold).

Math + glyph drawing primitives (``_draw_digit``, ``_draw_number``,
``_draw_line``, ``_draw_box``, ``_e_per_mm``) are reused from
``calib_pa_pattern_generator.py``. The only PA-Line-specific
ingredients are:

- ``print_pa_lines`` orchestrator (BS Calib.cpp:435-490)
- Left-to-right digit mode (already added to the pattern generator)
- Line-width / layer-height constants (BS-hardcoded — independent of
  preset, matches the desktop PA Line wizard's output)

Math validation against BS shipped output for 0.4mm nozzle DDE
defaults (start=0, end=0.1, step=0.002 → 51 rows):

- bed extents are pulled from the sidecar preset; pattern centres
  inside ``bed_w × bed_h`` per BS ``generate_test`` (Calib.cpp:415-430).
- m_space_y = 3.5, m_length_short = 20, m_length_long ∈ [40, 40 + min(0,
  bed_w-120)] (clipped to 40 for ≤120 mm beds, else widens).
- prime line: vertical column on the left, height = num*3.5 mm.
- three-segment row: slow 20 → fast 40 → slow 20 across X at each Y row.
- glyph box on the right (filled, two perimeters) holds K labels.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from backend.app.services.calib_pa_pattern_generator import (
    DEFAULT_FILAMENT_DIAMETER_MM,
    DEFAULT_FILAMENT_FLOW_RATIO,
    DEFAULT_TRAVEL_SPEED_MM_MIN,
    DigitMode,
    Vec2,
    _convert_number_to_string,
    _draw_box,
    _draw_number,
    _DrawBoxOpts,
    _e_per_mm,
    _move_to,
    _Writer,
)

# BS-hardcoded constants from ``CalibPressureAdvanceLine`` (Calib.hpp:266-273).
HEIGHT_LAYER = 0.2  # m_height_layer
LINE_WIDTH = 0.6  # m_line_width
THIN_LINE_WIDTH = 0.44  # m_thin_line_width (Orca skips drawing thin indicator lines)
NUMBER_LINE_WIDTH = 0.48  # m_number_line_width
SPACE_Y = 3.5  # m_space_y
LENGTH_SHORT = 20.0  # m_length_short
LENGTH_LONG_BASE = 40.0  # m_length_long base value before bed-clamp

# Glyph box width (BS Calib.cpp:482 — ``number_spacing * 8 = 3.0 * 8 = 24`` mm).
# number_spacing = digit_segment_len (2.0) + digit_gap_len (1.0).
GLYPH_BOX_WIDTH_MM = 24.0

# BS-default speeds (Calib.cpp:2841 — derived from preset, but PA Line wizard
# defaults to outer_wall_speed * 60 fast / fast/4 slow). Sidecar preset wins
# at slice time; these are fallbacks the generator uses when the spec doesn't
# pass them through.
DEFAULT_FAST_SPEED_MM_S = 100.0
DEFAULT_SLOW_SPEED_MM_S = 25.0  # fast / 4
NUMBER_LABEL_SPEED_MM_MIN = 3600.0  # BS hardcodes 3600 mm/min for glyphs (Calib.cpp:486)


def _speed_adjust(speed_mm_s: float) -> float:
    return speed_mm_s * 60.0  # mm/s → mm/min (BS ``speed_adjust`` does the same)


@dataclass(frozen=True)
class PALineParams:
    """Inputs to PA Line generator. ``start_pa / step_pa / count`` come
    from the operator's wizard inputs; everything else is read from the
    active preset chain (via the calling builder) or defaults to the
    BS-shipped PA-Line wizard values."""

    start_pa: float
    step_pa: float
    count: int  # number of K rows; BS uses ceil((end-start)/step) + 1

    nozzle_diameter: float = 0.4
    filament_diameter: float = DEFAULT_FILAMENT_DIAMETER_MM
    filament_flow_ratio: float = DEFAULT_FILAMENT_FLOW_RATIO

    # Bed extents in mm (printable_area bbox). PA Line centres the
    # pattern inside this rectangle. Defaults match a 256×256 H2D plate
    # but the builder passes the resolved values from the printer preset.
    bed_size_x: float = 256.0
    bed_size_y: float = 256.0
    bed_origin_x: float = 0.0
    bed_origin_y: float = 0.0

    fast_speed_mm_s: float = DEFAULT_FAST_SPEED_MM_S
    slow_speed_mm_s: float = DEFAULT_SLOW_SPEED_MM_S

    draw_numbers: bool = True
    is_bbl: bool = True


def _glyph_box_x(start_x: float) -> float:
    """X position of the right-side filled glyph box (BS Calib.cpp:479)."""
    return start_x + LENGTH_SHORT + LENGTH_LONG_BASE + LENGTH_SHORT


def _number_label_x(box_x: float) -> float:
    """X start for K-value digits inside the glyph box (BS line 485 —
    box_x + 3 + m_line_width)."""
    return box_x + 3.0 + LINE_WIDTH


def _max_label_width(p: PALineParams) -> float:
    """Longest K-string drawn × digit segment length (informational —
    used by callers needing the rendered tab width). BS doesn't expose
    this either; PA Pattern computes a similar quantity to size its
    tab. PA Line uses a fixed-width filled box ``number_spacing*8`` mm
    wide regardless."""
    most_chars = 0
    for i in range(0, p.count, 2):
        s = _convert_number_to_string(p.start_pa + i * p.step_pa)
        most_chars = max(most_chars, len(s))
    return most_chars * 2.0 + (most_chars - 1) * 1.0  # SEG + GAP


def generate_pa_line_layer(params: PALineParams) -> str:
    """Generate the single-layer g-code that draws the PA Line test.

    Mirrors BS ``CalibPressureAdvanceLine::generate_test`` +
    ``print_pa_lines`` (Calib.cpp:415-490). Returns a single string
    intended to be the ``extra=`` payload of one ``<layer top_z=...>``
    entry in ``custom_gcode_per_layer.xml``.

    Layout (bed-centred):

    - Compute ``count = min(operator_count, (bed_h - 10) / SPACE_Y)``
      — BS clamps so the pattern can't overflow the plate.
    - ``length_long = LENGTH_LONG_BASE + min(bed_w - 120, 0)`` —
      widens on small (<120mm) beds, fixed at 40 for everything else.
    - Pattern centred: start_x = bed_min_x + (bed_w - 2*LENGTH_SHORT -
      length_long - 20) / 2; start_y = bed_min_y + (bed_h -
      count*SPACE_Y) / 2.
    - Prime line: vertical column at start_x, num*SPACE_Y mm tall.
    - Per K-row i ∈ [0, count): emit M900 K → slow segment → fast
      segment → slow segment, all on the same Y row.
    - Optional glyph box on the right (filled), then per-row K labels
      written at half-row Y on the labelled rows.
    """
    p = params

    # BS doesn't clamp count when caller already computed it. We still
    # mirror the clamp so a wildly over-large operator count caps to
    # what physically fits on the plate.
    count = min(p.count, max(1, int((p.bed_size_y - 10) / SPACE_Y)))

    # m_length_long grows on tiny beds (BS Calib.cpp:425). For Bambu
    # plates (≥ 180×180 mm) this collapses to the constant 40.
    length_long = LENGTH_LONG_BASE + min(p.bed_size_x - 120.0, 0.0)

    # True bbox-centred placement: BS's own formula
    # ``start_x = bed_min_x + (bed_w - LENGTH_SHORT*2 - length_long - 20) / 2``
    # only centres the 80-mm row-segment block and treats the glyph
    # box's 24 mm as an unaccounted "+20" pad on the right, which
    # off-centres the visual by ~4 mm. We compute the full visible
    # bbox (prime line + row segments + glyph box) and centre that
    # span on the bed instead — so the operator's eye lands on the
    # middle of the plate.
    pattern_x_span = LENGTH_SHORT * 2.0 + length_long
    if p.draw_numbers:
        pattern_x_span += GLYPH_BOX_WIDTH_MM

    # Y span: the prime line walks from start_y up to start_y +
    # count*SPACE_Y (top of stack), and when glyph box is drawn it
    # extends one SPACE_Y below start_y. So total Y span is
    # count*SPACE_Y + (SPACE_Y if drawing numbers else 0); start_y
    # sits SPACE_Y above the bottom of the visible bbox.
    pattern_y_span = count * SPACE_Y + (SPACE_Y if p.draw_numbers else 0.0)
    y_offset_below = SPACE_Y if p.draw_numbers else 0.0

    start_x = p.bed_origin_x + (p.bed_size_x - pattern_x_span) / 2.0
    start_y = p.bed_origin_y + (p.bed_size_y - pattern_y_span) / 2.0 + y_offset_below

    epm = _e_per_mm(LINE_WIDTH, HEIGHT_LAYER, p.filament_diameter, p.filament_flow_ratio)
    number_epm = _e_per_mm(NUMBER_LINE_WIDTH, HEIGHT_LAYER, p.filament_diameter, p.filament_flow_ratio)

    fast = _speed_adjust(p.fast_speed_mm_s)
    slow = _speed_adjust(p.slow_speed_mm_s)

    writer = _Writer(is_bbl=p.is_bbl, travel_speed_mm_min=DEFAULT_TRAVEL_SPEED_MM_MIN)

    out = "; start PA Line pattern\n"
    out += writer.travel_to_z(HEIGHT_LAYER, "Move to test Z")

    # Prime line — vertical column on the left, walking down through the
    # row stack. BS Calib.cpp:454-457.
    prime_x = start_x
    prime_y_top = start_y + count * SPACE_Y
    out += _move_to(writer, Vec2(prime_x, prime_y_top), "Prime: move to top")
    out += writer.set_speed(slow)
    out += writer.extrude_to_xy(Vec2(prime_x, start_y), epm * SPACE_Y * count * 1.2, "Prime: column")

    # K rows.
    for i in range(count):
        k = p.start_pa + i * p.step_pa
        y = start_y + i * SPACE_Y
        out += writer.set_pressure_advance(k)
        out += _move_to(writer, Vec2(start_x, y), f"K={k:.4f}: row start")
        out += writer.set_speed(slow)
        out += writer.extrude_to_xy(Vec2(start_x + LENGTH_SHORT, y), epm * LENGTH_SHORT, "Slow segment 1")
        out += writer.set_speed(fast)
        out += writer.extrude_to_xy(
            Vec2(start_x + LENGTH_SHORT + length_long, y),
            epm * length_long,
            "Fast segment",
        )
        out += writer.set_speed(slow)
        out += writer.extrude_to_xy(
            Vec2(start_x + LENGTH_SHORT + length_long + LENGTH_SHORT, y),
            epm * LENGTH_SHORT,
            "Slow segment 2",
        )

    # Reset PA to a neutral value once the sweep finishes (BS line 469 —
    # ``set_pressure_advance(m_is_bbl_bowden ? 0.4 : 0.0)``; we're DDE-only
    # so always 0).
    out += writer.set_pressure_advance(0.0)

    if not p.draw_numbers:
        out += "; end PA Line pattern\n"
        return out

    # Glyph box (filled, two perimeters) on the right of the row stack.
    # BS Calib.cpp:479-482 — width = number_spacing*8 mm, height
    # (count+1) * SPACE_Y, starting at (length_short + length_long +
    # length_short, start_y - SPACE_Y).
    box_start_x = _glyph_box_x(start_x)
    box_opts = _DrawBoxOpts(
        is_filled=True,
        num_perimeters=2,
        height=HEIGHT_LAYER,
        line_width=LINE_WIDTH,
        speed_mm_min=fast,
    )
    out += _draw_box(
        writer,
        box_start_x,
        start_y - SPACE_Y,
        # number_spacing * 8 = 3.0 * 8 = 24 mm (BS line 482)
        3.0 * 8.0,
        (count + 1) * SPACE_Y,
        box_opts,
        p.filament_diameter,
        p.filament_flow_ratio,
    )
    # Second layer for the labels — BS uses 2 × layer height to clear
    # the filled box top (Calib.cpp:483).
    out += writer.travel_to_z(HEIGHT_LAYER * 2.0, "Step Z up for labels")

    # K-value labels every other row, written left-to-right at the
    # vertical centre of the corresponding row.
    label_x = _number_label_x(box_start_x)
    for i in range(0, count, 2):
        k = p.start_pa + i * p.step_pa
        y = start_y + i * SPACE_Y + SPACE_Y / 2.0
        out += _draw_number(
            writer,
            label_x,
            y,
            k,
            NUMBER_LINE_WIDTH,
            number_epm,
            NUMBER_LABEL_SPEED_MM_MIN,
            DigitMode.LEFT_TO_RIGHT,
        )

    out += "; end PA Line pattern\n"
    return out


def num_lines_for_range(start: float, end: float, step: float) -> int:
    """Mirrors how BS computes ``count`` for ``generate_test`` (Calib.cpp:2845).

    ``llround(ceil((end - start) / step)) + 1`` — one row per increment
    plus the start row.
    """
    if step <= 0:
        raise ValueError("step must be > 0")
    span = end - start
    if span <= 0:
        raise ValueError("end must be > start")
    return int(math.ceil(span / step)) + 1
