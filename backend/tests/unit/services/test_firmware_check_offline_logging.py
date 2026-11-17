"""An unreachable Bambu Lab site is logged once per outage, not on every firmware check.

Every printer card asks again every few minutes; offline, each ask wrote an
ERROR for the page and a WARNING for the model, for as long as a tab stayed open.
"""

import logging
from types import SimpleNamespace

import pytest

from backend.app.services import firmware_check
from backend.app.services.firmware_check import FirmwareCheckService

LOGGER = "backend.app.services.firmware_check"


@pytest.fixture
def service(monkeypatch, tmp_path):
    svc = FirmwareCheckService()
    monkeypatch.setattr(svc, "_load_build_id_from_disk", lambda: (None, 0))
    monkeypatch.setattr(svc, "_save_build_id_to_disk", lambda build_id: None)
    return svc


def _down(svc, monkeypatch, error=None, status=None):
    async def get(url, *args, **kwargs):
        if error is not None:
            raise error
        return SimpleNamespace(status_code=status, text="")

    monkeypatch.setattr(svc, "_cf_client", SimpleNamespace(get=get))


def _up(svc, monkeypatch):
    async def get(url, *args, **kwargs):
        return SimpleNamespace(status_code=200, text='{"buildId":"abc123"}')

    monkeypatch.setattr(svc, "_cf_client", SimpleNamespace(get=get))


def _levels(caplog):
    return [r.levelno for r in caplog.records if r.name == LOGGER and r.levelno >= logging.INFO]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [{"error": OSError("no route")}, {"status": 503}])
async def test_an_outage_is_one_warning_however_often_it_is_asked(service, monkeypatch, caplog, failure):
    _down(service, monkeypatch, **failure)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        for _ in range(4):
            assert await service._get_build_id() is None

    assert _levels(caplog) == [logging.WARNING]
    assert service.download_page_unreachable is True


@pytest.mark.asyncio
async def test_coming_back_is_said_once_and_the_next_outage_is_new(service, monkeypatch, caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        _down(service, monkeypatch, error=OSError("no route"))
        await service._get_build_id()
        _up(service, monkeypatch)
        assert await service._get_build_id() == "abc123"
        service._build_id_time = 0  # the cached id has aged: ask the site again
        await service._get_build_id()
        _down(service, monkeypatch, error=OSError("no route"))
        service._build_id_time = 0
        await service._get_build_id()

    reachable = [r for r in caplog.records if r.name == LOGGER and "reachable again" in r.getMessage()]
    warnings = [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]
    assert len(reachable) == 1
    assert len(warnings) == 2


@pytest.mark.asyncio
async def test_a_model_with_no_answer_is_quiet_during_a_known_outage(service, monkeypatch, caplog):
    async def nothing(api_key):
        return None

    monkeypatch.setattr(service, "_fetch_version_from_wiki", nothing)
    monkeypatch.setattr(service, "_fetch_from_download_page", nothing)
    service._outages.add(firmware_check.FIRMWARE_PAGE_NAME)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert await service._fetch_firmware_versions("p1") is None

    assert _levels(caplog) == []
