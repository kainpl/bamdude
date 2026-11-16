"""The slicer sidecar's code version, and what the Slice dialog is told about it (G12, decision 6).

The sidecar images are built from the fork whenever the operator rebuilds
them, independently of this BamDude. The sidecar reports its own code version
on ``/health``; the backend states the version it needs, and the health answer
the Slice dialog's slicer cards read says when the running sidecar is older.
An older sidecar is still a healthy one: slicing goes on as far as it can.
"""

from __future__ import annotations

import httpx
import pytest

from backend.app.api.routes import slicer_presets
from backend.app.services import slicer_routing
from backend.app.services.slicer_api import (
    REQUIRED_SIDECAR_VERSION,
    sidecar_is_outdated,
    sidecar_version_of,
)


class TestReadingTheVersion:
    def test_the_version_is_the_health_bodys_sidecar_version(self):
        assert sidecar_version_of({"status": "healthy", "sidecarVersion": 3}) == 3

    def test_an_image_that_reports_none_has_no_version(self):
        assert sidecar_version_of({"status": "healthy", "checks": {}}) is None

    @pytest.mark.parametrize("value", ["3", 3.0, True, None, -1, [3]])
    def test_anything_but_a_whole_positive_number_is_no_version(self, value):
        assert sidecar_version_of({"sidecarVersion": value}) is None

    def test_a_body_that_is_not_an_object_has_no_version(self):
        assert sidecar_version_of(["sidecarVersion", 3]) is None
        assert sidecar_version_of(None) is None


class TestOlderThanNeeded:
    def test_no_version_is_older(self):
        assert sidecar_is_outdated(None) is True

    def test_a_lower_version_is_older(self):
        assert sidecar_is_outdated(REQUIRED_SIDECAR_VERSION - 1) is True

    def test_the_needed_version_and_any_later_one_are_not(self):
        assert sidecar_is_outdated(REQUIRED_SIDECAR_VERSION) is False
        assert sidecar_is_outdated(REQUIRED_SIDECAR_VERSION + 5) is False

    def test_the_backend_needs_a_real_version(self):
        assert isinstance(REQUIRED_SIDECAR_VERSION, int)
        assert REQUIRED_SIDECAR_VERSION >= 1


@pytest.fixture
def sidecar(monkeypatch):
    """Point the health route at a hand-built sidecar answer."""
    slicer_presets._health_cache.clear()

    async def resolve(db, *, slicer_override=None):
        return "orcaslicer", "http://sidecar:3003"

    monkeypatch.setattr(slicer_routing, "resolve_sidecar_url", resolve)
    answer: dict = {"status": 200, "json": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        if answer.get("error"):
            raise httpx.ConnectError("refused")
        return httpx.Response(answer["status"], json=answer["json"])

    real_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    yield answer
    slicer_presets._health_cache.clear()


def _health_body(**extra) -> dict:
    return {"status": "healthy", "checks": {"orcaslicer": {"available": True, "version": "2.5.0"}}, **extra}


class TestWhatTheSliceDialogIsTold:
    @pytest.mark.asyncio
    async def test_a_current_sidecar_is_not_called_outdated(self, sidecar):
        sidecar["json"] = _health_body(sidecarVersion=REQUIRED_SIDECAR_VERSION)
        payload = await slicer_presets.get_slicer_health("orcaslicer", db=None, _=None)
        assert payload["healthy"] is True
        assert payload["version"] == "2.5.0"
        assert payload["sidecar_version"] == REQUIRED_SIDECAR_VERSION
        assert payload["sidecar_required"] == REQUIRED_SIDECAR_VERSION
        assert payload["sidecar_outdated"] is False

    @pytest.mark.asyncio
    async def test_a_sidecar_that_reports_no_version_is_older_but_still_usable(self, sidecar):
        # Every image built before the version existed.
        sidecar["json"] = _health_body()
        payload = await slicer_presets.get_slicer_health("orcaslicer", db=None, _=None)
        assert payload["healthy"] is True
        assert payload["sidecar_version"] is None
        assert payload["sidecar_outdated"] is True

    @pytest.mark.asyncio
    async def test_a_lower_version_is_older(self, sidecar, monkeypatch):
        monkeypatch.setattr(slicer_presets, "REQUIRED_SIDECAR_VERSION", 4, raising=False)
        monkeypatch.setattr("backend.app.services.slicer_api.REQUIRED_SIDECAR_VERSION", 4)
        sidecar["json"] = _health_body(sidecarVersion=3)
        payload = await slicer_presets.get_slicer_health("orcaslicer", db=None, _=None)
        assert payload["healthy"] is True
        assert payload["sidecar_version"] == 3
        assert payload["sidecar_required"] == 4
        assert payload["sidecar_outdated"] is True

    @pytest.mark.asyncio
    async def test_an_unreachable_sidecar_is_offline_not_outdated(self, sidecar):
        # "Rebuild the containers" is advice for a sidecar that answered.
        sidecar["error"] = True
        payload = await slicer_presets.get_slicer_health("orcaslicer", db=None, _=None)
        assert payload["healthy"] is False
        assert not payload.get("sidecar_outdated")

    @pytest.mark.asyncio
    async def test_a_sidecar_that_answers_an_error_is_offline_not_outdated(self, sidecar):
        sidecar["status"] = 503
        sidecar["json"] = {"status": "unhealthy", "sidecarVersion": 0}
        payload = await slicer_presets.get_slicer_health("orcaslicer", db=None, _=None)
        assert payload["healthy"] is False
        assert not payload.get("sidecar_outdated")
