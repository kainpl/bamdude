"""Match macros to an (event, printer) tuple for future auto-execution.

Wiring
------
``macro_trigger.fire_event_macros`` calls :func:`find_macros_for_event` from
``main.on_print_start`` / ``on_print_complete``, and
``macro_trigger.fire_layer_macros`` calls :func:`find_layer_macros` from the
layer-change callback.

Matcher semantics
-----------------
A macro fires when **all** of:

* ``macro.event == event``
* ``macro.enabled is True``
* The macro's ``printer_models`` list contains ``"*"`` or the printer's
  model — spelled however the printer row spells it, because
  :func:`macro_targets_model` normalises it first.
* If ``macro.swap_mode_only`` is True, ``printer.swap_mode_enabled`` must
  also be True.
* Swap profile match: either the macro has no ``swap_profile`` (acts as a
  generic fallback) **or** its ``swap_profile`` equals the printer's
  currently-selected ``swap_profile``.

Multiple macros can match the same (event, printer) tuple - all of them
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
from backend.app.utils.printer_models import normalize_model_name

logger = logging.getLogger(__name__)

# The event whose macros fire mid-print, when the layer counter crosses
# ``Macro.trigger_layer``.
LAYER_REACHED_EVENT = "layer_reached"


def macro_targets_model(macro: Macro, model: str | None) -> bool:
    """Does this macro's ``printer_models`` cover *model*?

    ``macro.printer_models`` is JSON-encoded in storage, and the three ways that
    can go wrong (unparseable, not a list, ``"*"``) are decided here and nowhere
    else. Public because ``print_option_defaults`` narrows the same macro list
    for a queue row that has not been dispatched yet: a second reading of the
    same column is exactly the drift that makes a macro fire at one door and not
    another.

    ⚠️ **BOTH sides are NORMALISED here, so both doors ask the same question.**
    ``macro.printer_models`` normally holds the short names the macro editor
    writes ("X1C"), while a caller spells the model however its own source does:
    a ``Printer`` row can hold the long marketing name ("Bambu Lab X1 Carbon"),
    a 3MF says "C12". ``print_option_defaults`` normalised before calling and
    the fire-time filter did not, so a long-name printer was offered every
    macro at dispatch and then matched none of them when the event arrived —
    silently, because "no macro targets this printer" and "this printer fired
    nothing" look identical from outside.

    The STORED side is normalised too (finding M7). A macro whose column was
    written by anything but today's editor — an import, an older build, an API
    client, a hand-edited row — can hold "Bambu Lab X1 Carbon" itself, and
    normalising only the incoming model left it matching no printer at all.
    ``normalize_model_name`` returns what it does not recognise unchanged, so
    this is a no-op for every short name already in the column.
    """
    try:
        models = json.loads(macro.printer_models or "[]")
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(models, list):
        return False
    if "*" in models:
        return True
    normalized = normalize_model_name(model)
    targeted = {name for entry in models if (name := normalize_model_name(entry if isinstance(entry, str) else None))}
    return bool(normalized) and normalized in targeted


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
        if not macro_targets_model(macro, printer.model):
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


def find_layer_macros(
    printer: Printer,
    macros: Iterable[Macro],
    previous_layer: int,
    layer: int,
) -> list[Macro]:
    """Return the macros whose ``trigger_layer`` the print has just crossed.

    A crossing (``previous_layer < trigger_layer <= layer``), never an
    equality: MQTT reports do get dropped, so a print can go from layer 48
    straight to 52 — it has still passed 50, and the macro for 50 must run.

    A row with no ``trigger_layer`` is skipped rather than treated as 0. The
    API refuses to write one, but a hand-edited database should not make a
    macro fire on every single layer.
    """
    candidates = find_macros_for_event(LAYER_REACHED_EVENT, printer, macros)
    return [m for m in candidates if m.trigger_layer is not None and previous_layer < m.trigger_layer <= layer]
