"""A unit whose printer took the drying commands and never heated has its own message (upstream #3478).

The suspension after two parked cycles used to send the unproductive-humidity text («… this threshold cannot be
reached here»), which sends the operator to the threshold while the unit did not heat at all. The not-heating
template rides the same provider switch and the same event key: either way BamDude has stopped drying the unit.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.models.notification_template import DEFAULT_TEMPLATES
from backend.app.schemas.notification_template import EVENT_VARIABLES, SAMPLE_DATA
from backend.app.services.notification_service import NotificationService


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("never_heated", "template"),
    [(False, "ams_drying_suspended"), (True, "ams_drying_not_heating")],
)
async def test_the_template_is_picked_on_the_same_switch_and_event(never_heated, template):
    service = NotificationService()
    with (
        patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as get,
        patch.object(service, "_send_to_providers", new_callable=AsyncMock) as send,
        patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as build,
    ):
        get.return_value = [MagicMock()]
        build.return_value = ("t", "m")
        await service.on_ams_drying_suspended(1, "P2S", "HT-A", 75.0, 60.0, 2, AsyncMock(), never_heated=never_heated)

    assert get.await_args.args[1] == "on_ams_drying_suspended"
    assert build.await_args.args[1] == template
    # The delivery event stays the suspension's: Telegram chats and the inbox filter on it.
    assert send.await_args.args[4] == "ams_drying_suspended"


def test_the_default_template_gives_the_not_heating_advice():
    template = next(t for t in DEFAULT_TEMPLATES if t["event_type"] == "ams_drying_not_heating")
    body = template["body_template"]
    assert "did not heat" in body and "Stop Drying on the printer card" in body
    assert "threshold cannot be reached" not in body
    used = set(re.findall(r"\{(\w+)\}", template["title_template"] + " " + body))
    assert used <= set(EVENT_VARIABLES["ams_drying_not_heating"])
    assert set(EVENT_VARIABLES["ams_drying_not_heating"]) <= set(SAMPLE_DATA["ams_drying_not_heating"])
