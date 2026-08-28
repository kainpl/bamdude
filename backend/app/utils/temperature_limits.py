"""How hot a thing may be told to get, decided in one place.

BambuStudio spreads this across three files, and the pieces do not agree by
accident — each is a different question:

* ``StatusPanel.cpp`` holds the static fallbacks used before a printer has said
  anything: nozzle 20-300, bed 20-120, chamber 20-60.
* ``update_temp_ctrl`` replaces them from what the printer actually reported,
  and **the three do not replace the same ends** — see below.
* ``MachineObject::get_bed_temperature_limit`` decides the bed's ceiling from
  the machine's series and its mains voltage.

⚠️ **The bed's ceiling is lower on 220 V, not higher.** On the X1 and O series
BS returns 110 at 220 V and 120 otherwise. It reads as backwards until you
remember it is about the heater's element, not about available power. The
voltage is ``home_flag`` bit 3 (``parse_home_flag``).

⚠️ **Only the bed's MAXIMUM is ever overridden.** ``update_temp_ctrl`` calls
``SetMaxTemp(limit)`` for the bed and never touches its minimum, while for the
nozzle it sets both ends and only when the printer sent at least two values.
Copying the nozzle's shape onto the bed would quietly move a floor BS leaves
alone.

⚠️ **Zero is not in any of these ranges, and must always be allowed.** It is how
every one of these is turned off. BS lets it through with ``AddTemp(0)``, which
exempts a value from both the too-high and the too-low check — so a range whose
floor is 20 still accepts "off". A floor enforced without that exemption turns
"stop heating" into "heat to 20", which is the opposite request.
"""

from __future__ import annotations

# The value that means "stop heating". Exempt from every bound — BS's
# ``TempInput::AddTemp(0)``.
OFF = 0

# StatusPanel.cpp:2274-2327 — what the controls hold before a printer answers.
NOZZLE_RANGE_DEFAULT = (20, 300)
BED_RANGE_DEFAULT = (20, 120)

# ⚠️ Not StatusPanel's 20-60. That pair is the placeholder shown before
# connection; the moment ``update_temp_ctrl`` runs it is replaced by DevConfig's
# own defaults, which floor at 0 (``DevConfig.h``: ``m_chamber_temp_edit_min =
# 0``). Since we only ever answer for a connected printer, 0 is the honest
# floor, and it is also what every mirrored config actually carries.
CHAMBER_RANGE_DEFAULT = (0, 60)

# The one ceiling for the chamber targets that are NOT asked of a printer.
#
# ⚠️ Manual chamber control does **not** use this — :func:`limits_for` answers
# per model from the mirrored BS config, and that is the better answer. But the
# preheat filament map is global (one chamber target per filament type, shared
# by the whole farm) and the per-print override is entered before a printer is
# chosen, so neither has a model to ask. Those take the highest ceiling any
# model has and let the firmware clamp on the rest; a per-model maximum cannot
# be expressed in a global map.
#
# 60 was the X1E's ceiling, and the X1E was the only heated-chamber model when
# that limit was written. The H2 family and the X2D heat to 65, so the top of
# their range was simply unreachable — an ABS profile calling for 65 ran at 60.
# Pinned to the mirrored configs by ``test_chamber_ceiling.py``, so a BS re-sync
# that raises it fails a test instead of going unnoticed.
MAX_CHAMBER_TEMP_C = 65

# DeviceManager.hpp: ``#define BED_TEMP_LIMIT 120``.
BED_LIMIT_DEFAULT = 120
BED_LIMIT_X1_220V = 110

# BS's ``get_printer_series()`` folds both of these onto ``SERIES_X1``, and the
# bed rule keys off that — so the O series (H2 family) gets the voltage rule too.
_VOLTAGE_RULE_SERIES = frozenset({"series_x1", "series_o"})


def _reported_pair(reported: object) -> tuple[int, int] | None:
    """A printer-sent range, or ``None`` when it did not send a usable one.

    ⚠️ ``None`` and "sent something unusable" have to collapse together here,
    because BS's guard is a size check on a parsed vector — a range it could not
    parse never made it into the vector in the first place.
    """
    if not isinstance(reported, (list, tuple)) or len(reported) < 2:
        return None
    low, high = reported[0], reported[1]
    if not isinstance(low, int) or not isinstance(high, int) or isinstance(low, bool) or isinstance(high, bool):
        return None
    if high < low:
        return None
    return low, high


def nozzle_limits(reported_range: object = None) -> tuple[int, int]:
    """Both ends move together, and only when the printer sent two values —
    ``if (obj->nozzle_temp_range.size() >= 2)``."""
    return _reported_pair(reported_range) or NOZZLE_RANGE_DEFAULT


def bed_limits(
    *,
    series: str = "",
    is_220v: bool = False,
    reported_limit: int | None = None,
    reported_range: object = None,
) -> tuple[int, int]:
    """The bed's bounds, in BS's own order of precedence.

    ``get_bed_temperature_limit()`` first, then ``bed_temp_range[1]`` if the
    printer sent a range — and the floor stays at the static default throughout,
    because that is the one BS never overrides.
    """
    if series in _VOLTAGE_RULE_SERIES:
        ceiling = BED_LIMIT_X1_220V if is_220v else BED_LIMIT_DEFAULT
    elif reported_limit is not None and reported_limit >= 0:
        ceiling = reported_limit
    else:
        ceiling = BED_LIMIT_DEFAULT

    pair = _reported_pair(reported_range)
    if pair is not None:
        ceiling = pair[1]

    return BED_RANGE_DEFAULT[0], ceiling


def chamber_limits(config_range: object = None) -> tuple[int, int]:
    """From the model's ``support_chamber_temp_edit_range``, which our mirrored
    configs already carry ([0, 60] on the H2D, [0, 65] on the rest)."""
    return _reported_pair(config_range) or CHAMBER_RANGE_DEFAULT


def limits_for(model: str | None, state: object) -> dict[str, tuple[int, int]]:
    """All three bounds for one machine, from its model and its reported state.

    Takes the state rather than the MQTT client so the status projections — which
    only ever hold ``(model, state)`` — can reach the same answer the clamp uses.
    Two readings of this rule is how they drift apart, and the one that drifts is
    always the one nobody is looking at.
    """
    from backend.app.utils.printer_configs import chamber_temperature_range, printer_series

    return {
        "nozzle": nozzle_limits(getattr(state, "nozzle_temp_range", None)),
        "bed": bed_limits(
            series=printer_series(model),
            is_220v=bool(getattr(state, "is_220v", False)),
            reported_limit=getattr(state, "bed_temperature_limit", None),
            reported_range=getattr(state, "bed_temp_range", None),
        ),
        # ⚠️ The firmware version is passed even though every shipped config
        # carries this key in its base block today: without it the loader answers
        # from the 2023 base and would understate any model whose later firmware
        # moved or widened the range. Costs nothing — the loader is lru_cached on
        # exactly this pair.
        "chamber": chamber_limits(chamber_temperature_range(model, getattr(state, "firmware_version", None))),
    }


def clamp_target(value: int, limits: tuple[int, int]) -> int:
    """Cut a target down to the ceiling, the way BS does on send.

    ⚠️ The ceiling only. ``on_set_bed_temp`` / ``on_set_nozzle_temp`` clamp the
    maximum and say nothing about the minimum, because the input widget has
    already refused anything below it — so raising a low value here would invent
    a heat request BS never makes. And :data:`OFF` passes untouched, or the same
    line would turn "stop" into "warm".
    """
    if value == OFF:
        return OFF
    return min(value, limits[1])


def is_within(value: int, limits: tuple[int, int]) -> bool:
    """Whether a target is one the machine will accept, ``AddTemp(0)`` included.

    This is the widget's question (``TempInput`` refuses both ends and reports
    which bound was missed), kept apart from :func:`clamp_target`, which is the
    send path's. They disagree on purpose: a caller deserves to be told its
    request was out of range, and the wire still gets a value that cannot cook
    anything if the telling is skipped.
    """
    if value == OFF:
        return True
    return limits[0] <= value <= limits[1]
