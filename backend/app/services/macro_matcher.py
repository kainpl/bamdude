"""Match macros to an (event, printer) tuple for future auto-execution.

Status
------
**Not yet wired to runtime events.** This helper is scaffolding for the
planned "macros auto-fire on events" feature. Call sites that should
eventually hook it in (but do not today):

* ``backend/app/services/bambu_mqtt.py`` — print start / print complete /
  print failed transitions.
* ``backend/app/main.py`` queue orchestration — swap-mode start + table
  swap around queue item boundaries.
* Any future "on X" event bus.

When wiring, call :func:`find_macros_for_event` to resolve the set of
macros to run, then dispatch each one through the existing execute path
in ``api/routes/macros.py::execute_macro`` (refactor the gcode-sending
section into a shared helper at that time).

Matcher semantics
-----------------
A macro fires when **all** of:

* ``macro.event == event``
* ``macro.enabled is True``
* The macro's ``printer_models`` list contains ``"*"`` or the printer's
  exact model code.
* If ``macro.swap_mode_only`` is True, ``printer.swap_mode_enabled`` must
  also be True.
* Swap profile match: either the macro has no ``swap_profile`` (acts as a
  generic fallback) **or** its ``swap_profile`` equals the printer's
  currently-selected ``swap_profile``.

Multiple macros can match the same (event, printer) tuple — all of them
fire, in the order returned. If the operator wants "specific wins" semantics
later, filter the returned list to specific-first. We explicitly do not
collapse that at match time because a webhook-style generic macro and a
swap-specific gcode macro can legitimately both want to fire.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable

from backend.app.models.macro import Macro
from backend.app.models.printer import Printer

logger = logging.getLogger(__name__)


def _macro_targets_model(macro: Macro, model: str | None) -> bool:
    """``macro.printer_models`` is JSON-encoded in storage."""
    try:
        models = json.loads(macro.printer_models or "[]")
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(models, list):
        return False
    if "*" in models:
        return True
    return bool(model) and model in models


def find_macros_for_event(
    event: str,
    printer: Printer,
    macros: Iterable[Macro],
) -> list[Macro]:
    """Return the macros from ``macros`` that should fire for ``(event, printer)``.

    ``macros`` is intentionally passed in rather than queried here: callers
    often already have the full macro list cached, and passing it in keeps
    this function synchronous/pure so it's trivial to unit-test.
    """
    matched: list[Macro] = []
    for macro in macros:
        if macro.event != event:
            continue
        if not macro.enabled:
            continue
        if not _macro_targets_model(macro, printer.model):
            continue
        if macro.swap_mode_only and not printer.swap_mode_enabled:
            continue
        if macro.swap_profile is not None and macro.swap_profile != printer.swap_profile:
            continue
        matched.append(macro)

    if matched:
        logger.debug(
            "[MACRO-MATCH] event=%s printer=%s (model=%s, swap_profile=%s) -> %d macro(s): %s",
            event,
            printer.name,
            printer.model,
            printer.swap_profile,
            len(matched),
            [m.name for m in matched],
        )
    return matched
