"""Tri-state print-calibration mode: off / auto / on.

Shared Pydantic type + helpers for the three calibration toggles (bed
levelling, flow calibration, nozzle-offset calibration). See the SAFE spec
``docs/superpowers/specs/2026-07-22-tristate-print-calibration-SAFE.md``.

Design (§3.1): a single field accepts BOTH the legacy bool AND the new
tri-state string. A ``BeforeValidator`` coerces ``True → 'on'`` / ``False →
'off'`` so old API clients (which send a JSON bool) keep working unchanged,
while new clients send ``'off' | 'auto' | 'on'``.

The legacy bool column stays the source of truth for off/on; the ``*_mode``
column carries only the extra ``'auto'`` state. Routes use :func:`mode_to_bool`
to write the bool mirror and :func:`derive_mode` to read the effective mode.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BeforeValidator

# The three canonical values. Nothing else validates.
CalibrationModeStr = Literal["off", "auto", "on"]

_VALID = frozenset({"off", "auto", "on"})


def coerce_calibration_mode(v: object) -> object:
    """Normalize a calibration-mode input for the :data:`CalibrationMode` field.

    - legacy bool: ``True → 'on'`` / ``False → 'off'`` (old clients keep working)
    - str: trimmed + lowercased (validated against the ``Literal`` downstream)
    - ``None`` / anything else: passed through unchanged, so ``Optional`` fields
      keep ``None`` and genuinely-invalid values surface as a normal ``Literal``
      validation error rather than being silently swallowed here.
    """
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, str):
        return v.strip().lower()
    return v


CalibrationMode = Annotated[CalibrationModeStr, BeforeValidator(coerce_calibration_mode)]


def mode_to_bool(mode: str | bool | None) -> bool:
    """Legacy bool mirror of a calibration mode: only ``'on'`` (or ``True``) is True.

    ``'auto'`` and ``'off'`` both map to ``False`` — matching BambuStudio, which
    sets ``task_bed_leveling = (getValue == "on")`` (i.e. False for auto). Routes
    write this into the legacy bool column so it stays authoritative for off/on
    while the ``*_mode`` column carries the extra ``'auto'`` state.
    """
    if isinstance(mode, bool):
        return mode
    return mode == "on"


def derive_mode(mode: str | None, legacy_bool: bool) -> str:
    """Resolve the effective tri-state for reads.

    An explicitly stored ``*_mode`` value wins; otherwise derive from the legacy
    bool (``True → 'on'`` / ``False → 'off'``). Mirrors the reader rule
    ``mode = <x>_mode or ('on' if <x> else 'off')`` — a ``NULL``/blank mode means
    "this row predates (or doesn't override) the tri-state, use the bool".
    """
    if mode in _VALID:
        return mode  # type: ignore[return-value]
    return "on" if legacy_bool else "off"
