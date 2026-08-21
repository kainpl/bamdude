"""Canonical catalog of MQTT-level printer commands exposed via macros.

Only commands in this catalog can be bound to an ``action_type='mqtt_action'``
macro. Each entry maps a stable string id (stored in ``Macro.mqtt_action``)
to a short i18n-key fragment for UI display and a runtime dispatcher that
takes a ``BambuMQTTClient`` and calls the appropriate method.

An entry may declare a single argument (``param``), stored alongside it in
``Macro.mqtt_action_param``. One grammar — id names the command, param names
the value — so a light, a speed and a future fan or temperature all read the
same way in the UI and in storage. The alternative, baking the value into the
id (``chamber_light_off``), only works while the value set is two items wide.

Keep the dispatch side tiny — everything non-trivial (retries, state
gating) lives on the MQTT client itself; this layer only translates a
named action into one of its methods.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from backend.app.services.bambu_mqtt import BambuMQTTClient


@dataclass(frozen=True)
class MQTTActionChoice:
    """One selectable value of a ``kind="choice"`` parameter."""

    value: str
    label: str
    i18n_key: str


@dataclass(frozen=True)
class MQTTActionParamSpec:
    """The single argument an action takes, described for UI + validation."""

    kind: str  # "choice" today; "int" is wired but unused
    i18n_key: str
    choices: tuple[MQTTActionChoice, ...] = ()
    default: str | None = None
    min_value: int | None = None
    max_value: int | None = None
    unit: str | None = None

    def is_valid(self, value: str | None) -> bool:
        if value is None:
            return False
        if self.kind == "choice":
            return any(c.value == value for c in self.choices)
        if self.kind == "int":
            try:
                number = int(value)
            except (TypeError, ValueError):
                return False
            if self.min_value is not None and number < self.min_value:
                return False
            return not (self.max_value is not None and number > self.max_value)
        return False

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "i18n_key": self.i18n_key,
            "default": self.default,
            "choices": [{"value": c.value, "label": c.label, "i18n_key": c.i18n_key} for c in self.choices],
            "min_value": self.min_value,
            "max_value": self.max_value,
            "unit": self.unit,
        }


@dataclass(frozen=True)
class MQTTMacroAction:
    """Catalog entry for a named, macro-triggerable MQTT command."""

    id: str
    # Short, human-readable label. Frontend uses ``i18n_key`` for localised
    # text; the ``label`` field is the English fallback and also what shows
    # up in logs.
    label: str
    i18n_key: str
    # Synchronously invokes the MQTT command. Returns ``True`` on success.
    # The second argument is the stored parameter, or None for actions that
    # declare no ``param``.
    dispatch: Callable[[BambuMQTTClient, str | None], bool]
    param: MQTTActionParamSpec | None = None


MQTT_MACRO_ACTIONS: dict[str, MQTTMacroAction] = {
    "chamber_light": MQTTMacroAction(
        id="chamber_light",
        label="Chamber light",
        i18n_key="chamberLight",
        dispatch=lambda client, param: client.set_chamber_light(param == "on"),
        param=MQTTActionParamSpec(
            kind="choice",
            i18n_key="lightState",
            default="off",
            choices=(
                MQTTActionChoice("on", "On", "on"),
                MQTTActionChoice("off", "Off", "off"),
            ),
        ),
    ),
    "print_speed": MQTTMacroAction(
        id="print_speed",
        label="Print speed",
        i18n_key="printSpeed",
        dispatch=lambda client, param: client.set_print_speed(int(param)),
        param=MQTTActionParamSpec(
            kind="choice",
            i18n_key="speedLevel",
            default="2",
            choices=(
                # BambuStudio ``DevPrintingSpeedLevel`` (DevDefs.h): 1 silence,
                # 2 normal, 3 rapid, 4 rampage. BS offers all four on every
                # model and the mirrored printer configs carry no speed flag,
                # so this list is static, not derived from the model.
                MQTTActionChoice("1", "Silent", "silent"),
                MQTTActionChoice("2", "Standard", "standard"),
                MQTTActionChoice("3", "Sport", "sport"),
                MQTTActionChoice("4", "Ludicrous", "ludicrous"),
            ),
        ),
    ),
}

# Ids from before the light actions were folded into the one-grammar catalog.
# Rows are rewritten by migration m134 and the API normalises on write, so
# these should never be reached in practice — but a database restored from an
# older backup can still carry them, and silently turning such a macro into a
# no-op would be worse than one dict lookup.
_LEGACY_ACTION_ALIASES: dict[str, tuple[str, str]] = {
    "chamber_light_on": ("chamber_light", "on"),
    "chamber_light_off": ("chamber_light", "off"),
}


def resolve_action(action_id: str, param: str | None = None) -> tuple[MQTTMacroAction | None, str | None]:
    """Resolve an id (canonical or legacy) plus the parameter to dispatch with.

    Returns ``(None, None)`` for an unknown id. A legacy id carries its own
    value and ignores *param*; a canonical id with no param falls back to the
    spec's default rather than dispatching ``None``.
    """
    alias = _LEGACY_ACTION_ALIASES.get(action_id)
    if alias is not None:
        canonical_id, forced_param = alias
        return MQTT_MACRO_ACTIONS.get(canonical_id), forced_param

    action = MQTT_MACRO_ACTIONS.get(action_id)
    if action is None:
        return None, None
    if param is None and action.param is not None:
        return action, action.param.default
    return action, param


def get_action(action_id: str) -> MQTTMacroAction | None:
    action, _ = resolve_action(action_id)
    return action


def catalog_for_meta() -> list[dict]:
    """Return a JSON-ready list for the ``/macros/meta`` endpoint."""
    return [
        {
            "id": a.id,
            "label": a.label,
            "i18n_key": a.i18n_key,
            "param": a.param.as_dict() if a.param is not None else None,
        }
        for a in MQTT_MACRO_ACTIONS.values()
    ]
