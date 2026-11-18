"""Failure detection does not poll before the web server serves requests.

The ML API fetches each frame from BamDude by URL. The detection loop is started inside the lifespan,
before the server accepts a request, so the first poll of every start asked the ML API to fetch a
frame from a server that did not answer yet: one ``400 BAD REQUEST`` per start, for the first printer
polled (farm log, 2026-11-18). The loop is told how to ask whether the server is serving and waits.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.app.services import obico_detection
from backend.app.services.obico_detection import ObicoDetectionService


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(obico_detection, "_SERVING_POLL_SECONDS", 0.01)
    svc = ObicoDetectionService()
    polls: list[int] = []

    async def _settings():
        return {"enabled": True, "ml_url": "http://ml.invalid", "poll_interval": 5, "save_detections": False}

    async def _poll(settings):
        polls.append(1)

    monkeypatch.setattr(svc, "_load_settings", _settings)
    monkeypatch.setattr(svc, "_poll_once", _poll)
    svc.polls = polls
    return svc


async def _stop(svc):
    task = svc._task
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_no_poll_before_the_server_serves(service):
    serving = [False]
    await service.start(serving=lambda: serving[0])
    try:
        await asyncio.sleep(0.05)
        assert service.polls == []

        serving[0] = True
        await asyncio.sleep(0.05)
        assert service.polls == [1]
    finally:
        await _stop(service)


async def test_a_service_started_without_the_question_polls_at_once(service):
    await service.start()
    try:
        await asyncio.sleep(0.05)
        assert service.polls == [1]
    finally:
        await _stop(service)
