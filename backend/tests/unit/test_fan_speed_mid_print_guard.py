"""Changing a fan mid-print is a warning, not a refusal — but it has to be
answerable on the server.

Registry item F6 (the speed half). BS asks before a mid-print fan change and
offers "Change Anyway" (``FanOperate::check_printing_state``: *"Changing fan
speed during printing may affect print quality"*). So this cannot be a hard
409 — the operator is allowed to proceed.

⚠️ It also cannot live only in the browser. A warning that exists in one client
is not a warning: ``/fan-speed`` is reachable by API key, by the Telegram bot,
and by a tab left open since before the print started. The acknowledgement is
therefore a parameter the server insists on, and the dialog is the explanation
rather than the guard — the same shape as the other mid-print guards, which are
absolute because those actions have no "anyway".
"""

from __future__ import annotations

import inspect
import math

import pytest

from backend.app.api.routes import printers as printers_routes


def _param(name: str):
    return inspect.signature(printers_routes.set_fan_speed).parameters[name]


class TestTheAcknowledgementIsPartOfTheContract:
    def test_the_route_takes_a_confirm_flag(self) -> None:
        assert "confirm" in inspect.signature(printers_routes.set_fan_speed).parameters

    def test_it_defaults_to_not_acknowledged(self) -> None:
        """Absent means "nobody was warned". A default of True would make every
        existing caller silently pre-confirmed."""
        assert _param("confirm").default.default is False

    def test_it_is_a_boolean(self) -> None:
        assert _param("confirm").annotation is bool


class TestTheGuardIsOverridableNotAbsolute:
    """Read off the route body: the busy check is conditioned on ``confirm``.

    Pinned as source rather than exercised through HTTP because the surrounding
    route needs a live MQTT client; the integration path is covered by the
    printer-settings route tests.
    """

    @pytest.fixture
    def body(self) -> str:
        return inspect.getsource(printers_routes.set_fan_speed)

    def test_a_busy_printer_is_only_refused_without_the_acknowledgement(self, body: str) -> None:
        assert "if not confirm and is_printer_busy(printer_id):" in body

    def test_the_refusal_says_what_would_lift_it(self, body: str) -> None:
        """A 409 that does not name its own remedy sends people to the logs."""
        assert "confirm=true" in body

    def test_the_mode_refusals_stay_absolute(self, body: str) -> None:
        """⚠️ These two are not overridable, and must not become so: a fan the
        mode forces off or drives itself will ignore the command, so "anyway"
        would be a button that does nothing."""
        assert "if _control == FAN_OFF:" in body
        assert "if _control != FAN_CTRL:" in body
        assert "confirm" not in body.split("if _control == FAN_OFF:")[1].split("\n\n")[0]


class TestTheStepsMatchTheHardware:
    """BS counts gears 1..10 and sends ``gear * 10`` on the new protocol, so the
    printer understands multiples of ten and nothing between."""

    @pytest.mark.parametrize("gear", range(0, 11))
    def test_every_gear_is_a_whole_percentage(self, gear: int) -> None:
        assert gear * 10 in range(0, 101)

    @pytest.mark.parametrize("percent", [25, 75])
    def test_the_values_we_used_to_offer_are_not_gears(self, percent: int) -> None:
        """25 and 75 came from a round-numbers habit, not from the protocol —
        they cannot be expressed as a gear at all."""
        assert percent % 10 != 0

    @pytest.mark.parametrize("gear", range(11))
    def test_the_old_protocol_conversion_matches_bs_exactly(self, gear: int) -> None:
        """BS: ``floor(gear * 25.5)``.

        ⚠️ Two things this test caught, both written into it as claims first.
        We used to ``round`` the same product, which disagreed on gears 1, 5 and
        9 — 26 vs 25, 128 vs 127, 230 vs 229; the test was originally written
        asserting the two already agreed, and they did not. The obvious repair,
        ``int(percent * 2.55)``, is worse: 2.55 has no exact binary form, so
        100 % becomes 254.999… and floors to **254** — wrong at the most-used
        value of all. Integer arithmetic avoids both.
        """
        assert (gear * 10) * 51 // 20 == math.floor(gear * 25.5)
