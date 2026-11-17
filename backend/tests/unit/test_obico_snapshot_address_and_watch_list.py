"""Where the ML API fetches a snapshot from, and who is on the watch list."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from backend.app.models.settings import Settings
from backend.app.schemas.settings import AppSettingsUpdate
from backend.app.services import obico_detection
from backend.app.services.obico_detection import ObicoDetectionService, drop_printer_from_watch_list


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("", ""),
        ("  ", ""),
        ("http://bamdude:8000", "http://bamdude:8000"),
        (" https://farm.lan/bamdude/ ", "https://farm.lan/bamdude"),
        ("http://192.168.1.20:8000", "http://192.168.1.20:8000"),
        ("http://[fd00::20]:8000", "http://[fd00::20]:8000"),
    ],
)
def test_an_address_of_ours_is_accepted_private_ones_included(given, stored):
    assert AppSettingsUpdate(obico_snapshot_base_url=given).obico_snapshot_base_url == stored


@pytest.mark.parametrize(
    "given",
    [
        "bamdude:8000",
        "ftp://bamdude:8000",
        "http://",
        "http://bam dude:8000",
        "http://bamdude:99999",
        "http://user:secret@bamdude:8000",
        "http://bamdude:8000/?next=1",
        "http://bamdude:8000/#top",
    ],
)
def test_what_the_ml_api_could_not_fetch_from_is_refused_on_save(given):
    with pytest.raises(ValidationError):
        AppSettingsUpdate(obico_snapshot_base_url=given)


@pytest.mark.parametrize("bad", ["[true]", "[0]", "[-1]", "[1.5]", '["1"]'])
def test_the_watch_list_holds_printer_ids_only(bad):
    with pytest.raises(ValidationError):
        AppSettingsUpdate(obico_enabled_printers=bad)
    assert AppSettingsUpdate(obico_enabled_printers="[1, 2]").obico_enabled_printers == "[1, 2]"


async def _settings(db_session, monkeypatch, **rows):
    for key, value in rows.items():
        db_session.add(Settings(key=key, value=value))
    await db_session.commit()

    class _Session:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(obico_detection, "async_session", _Session)
    return await ObicoDetectionService()._load_settings()


@pytest.mark.asyncio
async def test_the_address_for_the_ml_api_wins_over_the_external_url(db_session, monkeypatch):
    settings = await _settings(
        db_session,
        monkeypatch,
        external_url="https://public.example/",
        obico_snapshot_base_url="http://bamdude:8000/",
    )
    assert settings["snapshot_base_url"] == "http://bamdude:8000"
    assert settings["external_url"] == "https://public.example"


@pytest.mark.asyncio
async def test_without_it_the_external_url_is_used(db_session, monkeypatch):
    settings = await _settings(db_session, monkeypatch, external_url="https://public.example/")
    assert settings["snapshot_base_url"] == "https://public.example"


@pytest.mark.asyncio
async def test_the_ml_api_is_handed_the_snapshot_at_that_address(monkeypatch):
    service = ObicoDetectionService()
    monkeypatch.setattr(service, "_capture_frame", AsyncMock(return_value=b"\xff\xd8\xff\xd9"))
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = lambda: {"detections": []}
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("backend.app.services.obico_detection.httpx.AsyncClient", lambda **_kw: client)

    await service._check_printer(
        1,
        SimpleNamespace(state="RUNNING", task_name="job"),
        {
            "ml_url": "http://ml:3333",
            "sensitivity": "medium",
            "action": "notify",
            "external_url": "https://public.example",
            "snapshot_base_url": "http://bamdude:8000",
        },
    )
    assert client.get.await_args.kwargs["params"]["img"].startswith("http://bamdude:8000/api/v1/obico/cached-frame/")


@pytest.mark.parametrize(
    ("before", "after"),
    [("[3, 5]", "[3]"), ("[5]", "[]"), ("[3]", "[3]"), ("", ""), ("not json", "not json")],
)
@pytest.mark.asyncio
async def test_a_deleted_printer_leaves_the_watch_list(db_session, before, after):
    db_session.add(Settings(key="obico_enabled_printers", value=before))
    await db_session.commit()
    await drop_printer_from_watch_list(db_session, 5)
    await db_session.commit()
    value = (await db_session.execute(select(Settings.value).where(Settings.key == "obico_enabled_printers"))).scalar()
    assert value == after
    if after and after != "not json":
        assert 5 not in json.loads(value)


@pytest.mark.asyncio
async def test_no_watch_list_row_is_nothing_to_do(db_session):
    await drop_printer_from_watch_list(db_session, 5)
