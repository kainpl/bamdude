"""Regression tests for D.18, D.19, D.20, D.21 — AMS tray_state signal handling.

Three coordinated upstream fixes from the 0.2.4.2 wave land together in
``bambu_mqtt._handle_ams_data`` + ``printer_manager.printer_state_to_dict``:

* **D.18 (upstream `7d3af983` + `e2df0fc6`):** bare-tray payload
  ``{"id": N}`` (post-restart) and the populated payload with
  ``tray_exist_bits`` bit cleared (steady-state) both promote the slot
  to ``state=9`` so downstream readers see one canonical "no spool"
  signal instead of guessing from absent fields.

* **D.21 (upstream `7aa5ff01`):** the ``power_on_flag=False`` guard
  added in #765 was over-broad — some X1C firmware reports
  ``power_on_flag=False`` while idle between prints, with
  ``tray_exist_bits`` still tracking real slot inventory. Skip the
  bitmask update only for the exact shutdown pattern (zero bits AND
  ``power_on_flag=False``). Non-zero bits get applied even when
  ``power_on_flag=False`` so spool-removal detection works.
"""

from __future__ import annotations

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.printer_manager import printer_state_to_dict


def _make_state(raw_data: dict) -> PrinterState:
    """Real ``PrinterState`` instance — uses dataclass defaults for every
    field the serializer touches; we only override ``raw_data``."""
    state = PrinterState()
    state.raw_data = raw_data
    return state


class TestBareTrayHeuristic:
    """``printer_state_to_dict`` belt-and-suspenders: a ``{"id": N}`` tray
    payload (no state, no anything else) is promoted to ``state=9``. Steady-
    state populated payloads with ``state`` already set stay untouched."""

    def test_bare_tray_emulates_state_9(self):
        state = _make_state(
            {
                "ams": [
                    {
                        "id": 0,
                        "tray": [
                            {"id": 0},  # bare tray — must promote to state=9
                            {"id": 1, "state": 11, "tray_type": "PLA"},  # loaded sibling
                        ],
                    }
                ]
            }
        )
        dumped = printer_state_to_dict(state)
        trays = dumped["ams"][0]["tray"]
        assert trays[0]["state"] == 9, "bare {'id': N} must be promoted to state=9"
        assert trays[1]["state"] == 11, "loaded sibling slot keeps its state untouched"

    def test_populated_payload_with_state_3_is_not_promoted(self):
        """Post-Reset-Slot A1 Mini BMCU sends ``state=3, tray_type=""`` with a
        physically-loaded spool. That MUST NOT be promoted to state=9 —
        the #1322 root fix keeps the MQTT push firing in that case."""
        state = _make_state(
            {
                "ams": [
                    {
                        "id": 0,
                        "tray": [
                            {"id": 0, "state": 3, "tray_type": ""},
                        ],
                    }
                ]
            }
        )
        dumped = printer_state_to_dict(state)
        trays = dumped["ams"][0]["tray"]
        assert trays[0]["state"] == 3, "populated payload with state=3 stays as-is"

    def test_loaded_tray_with_state_11_passes_through(self):
        state = _make_state(
            {
                "ams": [
                    {
                        "id": 0,
                        "tray": [{"id": 0, "state": 11, "tray_type": "PLA", "tray_color": "FF0000FF"}],
                    }
                ]
            }
        )
        dumped = printer_state_to_dict(state)
        assert dumped["ams"][0]["tray"][0]["state"] == 11

    def test_bare_tray_state_is_integer_not_string(self):
        """Downstream check in ``inventory.py`` uses ``tray_state == 9``
        (not ``in {"9", 9}``) — a string would silently miss."""
        state = _make_state({"ams": [{"id": 0, "tray": [{"id": 0}]}]})
        dumped = printer_state_to_dict(state)
        assert dumped["ams"][0]["tray"][0]["state"] == 9
        assert isinstance(dumped["ams"][0]["tray"][0]["state"], int)
