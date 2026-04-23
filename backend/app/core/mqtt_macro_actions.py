"""Canonical catalog of MQTT-level printer commands exposed via macros.

Only commands in this catalog can be bound to an ``action_type='mqtt_action'``
macro. Each entry maps a stable string id (stored in ``Macro.mqtt_action``)
to a short i18n-key fragment for UI display and a runtime dispatcher that
takes a ``BambuMQTTClient`` and calls the appropriate method.

Keep the dispatch side tiny — everything non-trivial (retries, state
gating) lives on the MQTT client itself; this layer only translates a
named action into one of its methods.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from backend.app.services.bambu_mqtt import BambuMQTTClient


@dataclass(frozen=True)
class MqttMacroAction:
    """Catalog entry for a named, macro-triggerable MQTT command."""

    id: str
    # Short, human-readable label. Frontend uses ``i18n_key`` for localised
    # text; the ``label`` field is the English fallback and also what shows
    # up in logs.
    label: str
    i18n_key: str
    # Synchronously invokes the MQTT command. Returns ``True`` on success.
    dispatch: Callable[[BambuMQTTClient], bool]


MQTT_MACRO_ACTIONS: dict[str, MqttMacroAction] = {
    "chamber_light_off": MqttMacroAction(
        id="chamber_light_off",
        label="Chamber light off",
        i18n_key="chamberLightOff",
        dispatch=lambda client: client.set_chamber_light(False),
    ),
    "chamber_light_on": MqttMacroAction(
        id="chamber_light_on",
        label="Chamber light on",
        i18n_key="chamberLightOn",
        dispatch=lambda client: client.set_chamber_light(True),
    ),
}


def get_action(action_id: str) -> MqttMacroAction | None:
    return MQTT_MACRO_ACTIONS.get(action_id)


def catalog_for_meta() -> list[dict]:
    """Return a JSON-ready list for the ``/macros/meta`` endpoint."""
    return [{"id": a.id, "label": a.label, "i18n_key": a.i18n_key} for a in MQTT_MACRO_ACTIONS.values()]
