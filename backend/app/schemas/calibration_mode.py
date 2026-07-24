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
    - int: the MQTT wire encoding ``0/1/2`` (see :func:`mode_to_int`). Not a shape
      our own clients ever sent — our legacy column was BOOLEAN — but an API
      caller mirroring the wire ints shouldn't get a validation error for a value
      we already understand everywhere else (upstream parity, #2e45893d).
    - str: trimmed + lowercased, and the legacy ``"true"``/``"false"``/``"0"``/
      ``"1"`` spellings mapped, then validated against the ``Literal`` downstream
    - ``None`` / anything else: passed through unchanged, so ``Optional`` fields
      keep ``None`` and genuinely-invalid values surface as a normal ``Literal``
      validation error rather than being silently swallowed here.
    """
    # bool must be checked before int — bool IS a subclass of int in Python.
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, int):
        return {0: "off", 1: "on", 2: "auto"}.get(v, v)
    if isinstance(v, str):
        low = v.strip().lower()
        return {"true": "on", "1": "on", "false": "off", "0": "off"}.get(low, low)
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


def normalize_mode(value: str | bool | None) -> str:
    """Coerce a legacy bool / arbitrary string into a canonical
    ``'off' | 'auto' | 'on'`` for persisting the ``*_mode`` column.

    ``True → 'on'`` / ``False → 'off'``; a valid tri-state string is lowercased;
    anything else (``None``, junk) → ``'off'`` (the safe default).
    """
    if isinstance(value, str):
        v = value.strip().lower()
        return v if v in _VALID else "off"
    if isinstance(value, bool):
        return "on" if value else "off"
    return "off"


def mode_to_int(mode: str | bool | None) -> int:
    """MQTT wire int for a calibration mode: ``off=0``, ``on=1``, ``auto=2``.

    Matches BambuStudio ``getValueInt()`` (SelectMachine.cpp:7176). Accepts the
    legacy bool (``True → 1`` / ``False → 0``). Unknown / ``None`` → ``0`` (off),
    the safe default.
    """
    if isinstance(mode, bool):
        return 1 if mode else 0
    return {"off": 0, "on": 1, "auto": 2}.get(mode, 0)


def clamp_auto(value: int, auto_supported: bool) -> int:
    """Downgrade ``auto`` (2) to ``on`` (1) on a model whose firmware lacks the
    auto mode, so ``2`` never reaches a non-supporting machine. ``off`` / ``on``
    (0 / 1) pass through unchanged."""
    return 1 if (value == 2 and not auto_supported) else value


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
