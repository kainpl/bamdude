"""The printer cards can see AI detection without seeing its configuration (#1546).

The live classification lived only behind ``/obico/status``, which requires
``settings:read`` — so putting a badge on the printer cards meant either handing
every operator the ML URL and the detection history, or leaving the badge out.

Hence a second, deliberately thin route: enabled, which printers are watched,
and the per-printer class. Nothing that describes the setup.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from backend.app.api.routes import obico as obico_routes
from backend.app.core.permissions import Permission
from backend.app.services.obico_detection import obico_detection_service


def _user(*permissions: str):
    """A stand-in for the authenticated user, answering only what the route asks."""
    return SimpleNamespace(has_permission=lambda p: p in permissions)


@pytest.fixture
def quiet_service(monkeypatch):
    """A service with a known error and no live prints."""
    monkeypatch.setattr(obico_detection_service, "_last_error", "connect to http://ml.internal:3333 failed")
    monkeypatch.setattr(obico_detection_service, "_states", {})
    monkeypatch.setattr(obico_detection_service, "_last_class", {})

    async def fake_settings():
        return {"enabled": True, "enabled_printers": {3, 1}}

    monkeypatch.setattr(obico_detection_service, "_load_settings", fake_settings)


@pytest.mark.asyncio
class TestWhatItExposes:
    async def test_it_carries_state_and_no_configuration(self, quiet_service) -> None:
        """The whole reason this is not just a widened ``/status``: those fields
        are the configuration, and an operator has no business reading them."""
        body = await obico_routes.get_printer_status(user=_user("printers:read"))

        assert set(body) == {"enabled", "monitored_printers", "per_printer", "last_error"}
        for leaked in ("ml_url", "sensitivity", "action", "poll_interval", "history", "thresholds"):
            assert leaked not in body, f"{leaked} is configuration and must stay behind settings:read"

    async def test_the_monitored_set_is_sorted(self, quiet_service) -> None:
        """The frontend does a membership test, but a stable order keeps the
        payload diffable and the cache key honest."""
        body = await obico_routes.get_printer_status(user=_user("printers:read"))

        assert body["monitored_printers"] == [1, 3]

    async def test_no_subset_configured_means_every_printer(self, monkeypatch) -> None:
        """``None`` is not "none monitored" — it is "no subset chosen, so all of
        them". A badge missing from every card would be the opposite reading."""

        async def all_printers():
            return {"enabled": True, "enabled_printers": None}

        monkeypatch.setattr(obico_detection_service, "_load_settings", all_printers)

        body = await obico_routes.get_printer_status(user=_user("printers:read"))

        assert body["monitored_printers"] is None

    async def test_the_error_string_stays_behind_settings_read(self, quiet_service) -> None:
        """Error strings embed configured URLs — the ML API base, the external
        URL — so they are configuration wearing a different hat."""
        as_operator = await obico_routes.get_printer_status(user=_user("printers:read"))
        as_admin = await obico_routes.get_printer_status(user=_user("printers:read", "settings:read"))

        assert as_operator["last_error"] is None
        assert "ml.internal" in as_admin["last_error"]

    async def test_an_unauthenticated_deployment_still_sees_the_error(self, quiet_service) -> None:
        """With auth off there is nobody to withhold it from, and hiding it would
        make a single-user install harder to debug for no gain."""
        body = await obico_routes.get_printer_status(user=None)

        assert "ml.internal" in body["last_error"]


class TestTheContract:
    def test_the_route_asks_for_printers_read(self) -> None:
        """The point of the route. If this ever becomes settings:read again the
        badge silently disappears for every operator — which is how it looked
        before the route existed."""
        source = inspect.getsource(obico_routes.get_printer_status)
        assert "Permission.PRINTERS_READ" in source
        assert "Permission.SETTINGS_READ" not in source.split('"""')[0]

    def test_per_printer_has_one_producer(self) -> None:
        """Split out of ``get_status`` rather than copied, so the settings panel
        and the printer cards cannot drift into two answers about one print."""
        assert Permission.PRINTERS_READ.value == "printers:read"
        assert "self.get_per_printer()" in inspect.getsource(obico_detection_service.get_status)
