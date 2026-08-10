"""A field the card renders has to travel by WebSocket, and has to be able to
trigger one.

Three separate projections of one printer state are maintained by hand:

======================================  ==========================================
``main.on_printer_status_change``'s status_key  what counts as "a change worth broadcasting"
``printer_manager.printer_state_to_dict``  what the WebSocket message carries
``api/routes/printers`` status response  what the card actually reads
======================================  ==========================================

They drift, and the drift is silent: a field missing from the key updates only
when something else happens to move, and a field missing from the payload
updates only on the next poll. That is what made a print-speed change on the
printer's own panel need a page refresh (registry L14).

⚠️ **This was made worse by a fix, not by neglect.** Once the card started
drawing its fans from ``airduct_fans`` alone, that list became the only source
for every fan on an air-duct machine — and it was in neither the key nor the
payload. Fan speeds on an X2D updated by poll only.

These tests do not demand the three be identical. Plenty of state is not worth
a broadcast. They pin the fields a card visibly depends on.
"""

from __future__ import annotations

import inspect

import pytest

from backend.app import main as main_module
from backend.app.services import printer_manager as pm_module

_STATUS_KEY = (
    inspect.getsource(main_module.on_printer_status_change).split("status_key = (", 1)[1].split("\n    )", 1)[0]
)
_WS_PAYLOAD = inspect.getsource(pm_module.printer_state_to_dict)


# Fields the printer card renders live. A change in any of them should reach the
# browser without a refetch.
LIVE_FIELDS = [
    "airduct_fans",
    "airduct_mode",
    "airduct_sub_mode",
    "cooling_fan_speed",
    "big_fan1_speed",
    "big_fan2_speed",
    "heatbreak_fan_speed",
    "chamber_light",
]


class TestTheWebsocketCarriesWhatTheCardDraws:
    @pytest.mark.parametrize("field", LIVE_FIELDS)
    def test_it_is_in_the_payload(self, field: str) -> None:
        assert f'"{field}"' in _WS_PAYLOAD, (
            f"{field} is rendered by the card but absent from the WebSocket payload — "
            "it would update only on the next poll"
        )


class TestAChangeCanTriggerABroadcast:
    @pytest.mark.parametrize("field", LIVE_FIELDS)
    def test_it_influences_the_dedup_key(self, field: str) -> None:
        """``on_printer_status_change`` returns early when the key is unchanged, so a
        field outside it cannot cause a message at all."""
        # airduct_fans is represented by a signature over the parts it is built
        # from — the list itself is derived, and hashing the derived objects
        # would rebuild them on every push.
        needle = "airduct_key" if field == "airduct_fans" else field
        assert needle in _STATUS_KEY, f"{field} does not appear in status_key — changing it alone broadcasts nothing"


class TestTheFanSignatureIsAboutSpeeds:
    def test_it_covers_the_part_state(self) -> None:
        """Part id alone would not notice a speed change; the state is the value
        the badge shows."""
        source = inspect.getsource(main_module.on_printer_status_change)
        assert 'get("state")' in source.split("airduct_key = ", 1)[1].split("\n    status_key", 1)[0]

    def test_it_is_ordered(self) -> None:
        """Dict order follows whatever the printer sent, so an unsorted tuple
        would differ between two identical states and broadcast on every push."""
        source = inspect.getsource(main_module.on_printer_status_change)
        assert "sorted(" in source.split("airduct_key = ", 1)[1].split("\n    status_key", 1)[0]
