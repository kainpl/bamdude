"""Shared reading of the firmware's own AMS drying state.

A leaf module on purpose: ``services/bambu_mqtt`` (the drying-cycle end
detector) and ``main.record_ams_history`` (the temperature alarm) both read it,
and ``drying_preflight`` — the natural home — imports ``printer_manager``, which
imports ``bambu_mqtt``. Nothing here imports from the app.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

# AMS ``dry_status`` (bits 4-7 of the per-unit ``info`` hex, BambuStudio
# ``DevFilaSystem.cpp`` / ``DevAms::DryStatus``): 0=Off, 1=Checking, 2=Drying,
# 3=Cooling, 4=Stopping, 5=Error, 6=HeatOutOfControl, 7=PrdTesting. The phases in
# which a cycle is still live — so a ``dry_time`` of 0 is not yet its end, and
# heat is heat the user asked for.
#
# ⚠️ Deliberately NOT BS's ``AmsIsDrying()``, which answers a different question
# — "should the UI show this unit as drying" — and therefore counts Error(5) and
# HeatOutOfControl(6) while excluding Cooling(3). Ours is "is the cycle still
# live": Stopping(4) and Error(5) are endings, cooling down is not one yet, and a
# heater out of control (6) must NEVER read as expected heat — that is exactly
# when a high-temperature alarm has to reach the user (upstream #1802).
ACTIVE_DRY_STATUSES = frozenset({1, 2, 3})  # Checking, Drying, Cooling

# A live countdown ticks once a minute. A ``dry_time`` that has not changed for
# this long, with no live phase vouching for it, is a timer with no cycle behind
# it: the printer took the command and is not heating (its power budget is
# spent, a print is running), or a cycle was paused partway. Two missed ticks
# plus jitter (upstream #2896 / #3096, seen on an H2D with three AMS-HT).
DRY_COUNTDOWN_STALL_SECONDS = 150


def is_countdown_parked(ams_data: Any) -> bool:
    """True when this AMS unit holds a drying timer that is not running.

    ``dry_countdown_stalled`` is stamped by the MQTT client (the only place that
    sees time pass between reports). Such a timer never reaches 0 by itself, so
    nothing that waits for drying to end may wait on it, and nothing may take
    it for heat somebody asked for.
    """
    if not isinstance(ams_data, Mapping) or not ams_data.get("dry_countdown_stalled"):
        return False
    try:
        return int(ams_data.get("dry_time") or 0) > 0
    except (TypeError, ValueError):
        return False


def running_dry_minutes(ams_data: Any) -> int:
    """Minutes left on a countdown that is actually running - 0 for a parked one."""
    if not isinstance(ams_data, Mapping) or is_countdown_parked(ams_data):
        return 0
    try:
        return max(0, int(ams_data.get("dry_time") or 0))
    except (TypeError, ValueError):
        return 0


def only_parked_timers(ams_units: Any) -> bool:
    """True when the only drying this printer shows is timers that are parked.

    False when nothing shows a timer at all: a command just sent has not been
    reported back yet, and that is drying about to begin.
    """
    units = [unit for unit in (ams_units or []) if isinstance(unit, Mapping)]
    return any(is_countdown_parked(unit) for unit in units) and not any(is_drying_active(unit) for unit in units)


def is_drying_active(ams_data: Any) -> bool:
    """True when this AMS unit reports a drying cycle in progress.

    Two independent signals, because neither alone is enough. ``dry_time`` is
    minutes remaining and reads 0 through the cooling phase that closes a cycle;
    ``dry_status`` covers that phase but is present only when the firmware sent a
    parseable ``info`` field.

    A parked timer (``is_countdown_parked``) is not a cycle: the unit is not
    heating, so the alarm, the queue hold and a scheduled start read it as idle.
    """
    if not isinstance(ams_data, Mapping):
        return False
    if is_countdown_parked(ams_data):
        return False
    try:
        if int(ams_data.get("dry_time") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass  # Unparseable countdown — fall through to the phase field
    try:
        return int(ams_data["dry_status"]) in ACTIVE_DRY_STATUSES
    except (KeyError, TypeError, ValueError):
        return False


def temperature_alarm_suppressed(
    *,
    drying_active: bool,
    temperature: float | None,
    threshold: float,
    latched_at: datetime | None,
    now: datetime,
    grace_minutes: int,
) -> tuple[bool, datetime | None]:
    """Decide whether to hold back the AMS high-temperature alarm (upstream #1802).

    Drying heats an AMS far past the alarm threshold by design — 45 °C for PLA,
    65 °C for PETG, up to 85 °C on an AMS-HT, against a 35 °C default — so without
    this the alarm fires once an hour for the whole cycle and keeps going while the
    unit cools back down.

    Returns ``(suppress, latched_at)``; the second element is the latch to
    persist — a timestamp while suppression is in force, ``None`` to clear it.

    Suppression ends as soon as the unit reads back at or below the threshold, not
    after a fixed delay, so a 65 °C cycle in a cold basement and a 45 °C one in a
    warm room each get the cool-down they need. ``grace_minutes`` only bounds a
    unit that never returns below the threshold — and one that stays that hot
    would have alarmed with no drying involved, so releasing there restores the
    ordinary behaviour instead of inventing a new alert.
    """
    if drying_active:
        return True, now
    if latched_at is None:
        return False, None
    # Back at a normal temperature: the cool-down is over. The only path that
    # clears the latch promptly, so it is checked before the cap.
    if temperature is not None and temperature <= threshold:
        return False, None
    # ``latched_at`` is never in the future: the caller either just stamped it
    # with this ``now`` or read it back through a loader that clamps — a future
    # stamp would hold suppression for the skew on top of the cap.
    if now - latched_at >= timedelta(minutes=grace_minutes):
        return False, None
    return True, latched_at
