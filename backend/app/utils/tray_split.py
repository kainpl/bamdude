"""Weight-split math for prints that traversed >1 AMS tray mid-print.

``state.tray_change_log`` records ``(global_tray_id, layer_num)`` tuples every
time ``tray_now`` changes during a print (written by ``bambu_mqtt.py``). At
completion, both the internal Spool inventory (``usage_tracker.py``) and the
Spoolman writer (``spoolman_tracking.py``) need to split a slot's total weight
across the segments those changes define — one call site per inventory
backend, one identical splitting algorithm.

The algorithm lives here so the two callers cannot drift: #1793 came from
``spoolman_tracking`` never carrying the split at all, while ``usage_tracker``
had shipped it since #957 and refined it in #1771. Sharing the helper is the
structural fix; each caller wraps its own "resolve segment tray → charge N
grams" side effect.
"""

from __future__ import annotations

# Qualified-name access (``threemf_tools.mm_to_grams(...)`` rather than
# ``from … import mm_to_grams``) so ``unittest.mock.patch`` on the
# threemf_tools module lands in the helper too — the pre-refactor
# ``usage_tracker`` call site imported at call-time inside a try block,
# which had the same testability property.
from backend.app.utils import threemf_tools


def compute_tray_split_grams(
    tray_changes: list[tuple[int, int]],
    total_weight: float,
    slot_id: int,
    layer_usage: dict[int, dict[int, float]] | None,
    density: float,
    diameter: float,
    total_layers: int,
    last_layer_num: int,
) -> list[tuple[int, int, float]]:
    """Split ``total_weight`` for a single slice slot across tray segments.

    ``tray_changes`` is the ordered list ``[(global_tray_id, seg_start_layer), ...]``
    exactly as it appears in ``state.tray_change_log``. The last segment runs
    to the end of the print; every other segment ends at the next entry's
    ``seg_start_layer``.

    Preference order for per-segment grams — matches ``usage_tracker`` so both
    inventory backends split identically:

    1. **G-code cumulative extrusion** (``layer_usage``, indexed by 0-based
       filament id). Precise: uses the mm actually consumed between
       ``seg_start_layer`` and the next segment's start, then converts via
       Spoolman-authoritative ``density`` / ``diameter``.
    2. **Linear layer-ratio** — ``total_weight * segment_layers / denom``, with
       ``denom = total_layers or last_layer_num``. Firmware on P1S (observed)
       resets ``total_layer_num`` to 0 at print end, so the captured
       ``last_layer_num`` is the durable denominator. #1771 addressed the
       pre-fix behaviour of dumping everything onto the last segment.
    3. **Equal-split** — when neither denominator is available (server restart
       mid-print, missing state). Wrong but bounded — the last segment absorbs
       any rounding drift via the ``is_last`` branch.

    Returns ``[(seg_idx, global_tray_id, segment_grams)]``. Empty when
    ``tray_changes`` is empty; the caller decides whether to fall through to
    single-tray attribution (``len(tray_changes) <= 1``).
    """
    if not tray_changes:
        return []

    grams = compute_layer_segment_grams(
        boundary_layers=[layer for _, layer in tray_changes],
        total_weight=total_weight,
        slot_id=slot_id,
        layer_usage=layer_usage,
        density=density,
        diameter=diameter,
        total_layers=total_layers,
        last_layer_num=last_layer_num,
    )
    return [
        (seg_idx, tray_global, g) for seg_idx, ((tray_global, _), g) in enumerate(zip(tray_changes, grams, strict=True))
    ]


def compute_layer_segment_grams(
    boundary_layers: list[int],
    total_weight: float,
    slot_id: int,
    layer_usage: dict[int, dict[int, float]] | None,
    density: float,
    diameter: float,
    total_layers: int,
    last_layer_num: int,
) -> list[float]:
    """Split ``total_weight`` across segments starting at ``boundary_layers``.

    The layer-boundary half of :func:`compute_tray_split_grams`, factored out
    so the runout journal (whose boundaries are spool changes on ONE tray, not
    tray changes) shares the identical preference order: G-code cumulative →
    linear layer-ratio → equal split, with the last segment absorbing rounding
    drift. Returns one grams figure per boundary, in order.
    """
    if not boundary_layers:
        return []

    filament_id = slot_id - 1  # 0-based for gcode
    n_segments = len(boundary_layers)
    denom = total_layers or last_layer_num
    grams: list[float] = []
    sum_previous = 0.0

    # How much of the slot's own gcode timeline was actually laid down. The
    # fractions below are fractions of the WHOLE plate ("exact at completion",
    # slot_progress_fraction's docstring), but a failed print's caller has
    # ALREADY shortened total_weight to estimate x progress — multiplying the
    # two applies the shortening twice to every segment but the last, which
    # then swallows the difference as if it were rounding drift. Dividing each
    # segment by the printed fraction restores the division; at completion the
    # fraction is 1.0 and this is a no-op. None (no gcode, or the last layer was
    # never captured) leaves the historical behaviour untouched.
    printed_fraction = None
    if layer_usage and last_layer_num > 0:
        printed_fraction = threemf_tools.slot_progress_fraction(layer_usage, filament_id, last_layer_num)
        if not printed_fraction or printed_fraction <= 0:
            printed_fraction = None

    for seg_idx, seg_start_layer in enumerate(boundary_layers):
        is_last = seg_idx + 1 >= n_segments

        if is_last:
            # Last segment: remainder to avoid rounding drift.
            segment_grams = total_weight - sum_previous
        elif layer_usage:
            # Proportional along the slot's own gcode timeline, scaled to the
            # total: absolute mm→grams silently dumped the whole flush budget
            # (which never appears as gcode extrusion) onto the LAST segment.
            seg_end_layer = boundary_layers[seg_idx + 1]
            f_start = threemf_tools.slot_progress_fraction(layer_usage, filament_id, seg_start_layer)
            f_end = threemf_tools.slot_progress_fraction(layer_usage, filament_id, seg_end_layer)
            if f_start is not None and f_end is not None:
                span = max(0.0, f_end - f_start)
                if printed_fraction is not None:
                    span = min(span / printed_fraction, 1.0)
                segment_grams = total_weight * span
            elif denom > 0:
                segment_grams = total_weight * (seg_end_layer - seg_start_layer) / denom
            else:
                segment_grams = total_weight / n_segments
        else:
            # No per-layer data: linear fallback by layer ratio (#1771).
            seg_end_layer = boundary_layers[seg_idx + 1]
            if denom > 0:
                segment_grams = total_weight * (seg_end_layer - seg_start_layer) / denom
            else:
                segment_grams = total_weight / n_segments

        sum_previous += segment_grams
        grams.append(segment_grams)

    return grams
