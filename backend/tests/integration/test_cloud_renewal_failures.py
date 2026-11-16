"""A renewal that went wrong on OUR side says nothing about the sign-in.

Two ways a renewal used to end as "your sign-in expired" although Bambu never
refused it: the refresh call raised something that is not a network error
(a shared HTTP client closed at shutdown), and the new pair could not be
written after Bambu had already issued it (a locked database). Neither marks
the sign-in invalid; both answer as an upstream failure (502).
"""

from __future__ import annotations

import pytest

from backend.app.services import bambu_cloud, bambu_cloud_credentials
from backend.tests.integration.test_cloud_unreachable import (  # noqa: F401 - ``bambu`` is a fixture
    BIND,
    EXPIRED,
    REFRESH,
    _sign_in,
    _stored,
    bambu,
)

pytestmark = pytest.mark.asyncio

RENEWED_PAIR = (200, {"accessToken": "acc-2", "refreshToken": "ref-2"})
DEVICES = (200, {"devices": []})


async def test_an_unexpected_error_during_the_refresh_is_not_a_refusal(async_client, db_session, bambu, monkeypatch):
    user_id = await _sign_in(db_session)
    bambu({("GET", BIND): EXPIRED})

    async def closed(self):
        raise RuntimeError("Cannot send a request, as the client has been closed.")

    monkeypatch.setattr(bambu_cloud.BambuCloudService, "refresh_access_token", closed)

    resp = await async_client.get("/api/v1/cloud/devices")

    assert resp.status_code == 502, resp.text
    assert await _stored(db_session, user_id) == ("acc", "ref", False)


async def test_a_pair_that_could_not_be_written_once_is_written_on_the_second_try(
    async_client, db_session, bambu, monkeypatch
):
    user_id = await _sign_in(db_session)
    fake = bambu({("GET", BIND): [EXPIRED, DEVICES], ("POST", REFRESH): RENEWED_PAIR})
    write, calls = bambu_cloud_credentials._persist_renewed, []

    async def locked_once(*args):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return await write(*args)

    monkeypatch.setattr(bambu_cloud_credentials, "_persist_renewed", locked_once)

    resp = await async_client.get("/api/v1/cloud/devices")

    assert resp.status_code == 200, resp.text
    assert fake.count("POST", REFRESH) == 1
    assert await _stored(db_session, user_id) == ("acc-2", "ref-2", False)


async def test_a_pair_that_cannot_be_written_is_a_502_and_marks_nothing(async_client, db_session, bambu, monkeypatch):
    user_id = await _sign_in(db_session)
    fake = bambu({("GET", BIND): [EXPIRED, DEVICES], ("POST", REFRESH): RENEWED_PAIR})

    async def locked(*_args):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(bambu_cloud_credentials, "_persist_renewed", locked)

    resp = await async_client.get("/api/v1/cloud/devices")

    assert resp.status_code == 502, resp.text
    assert fake.count("POST", REFRESH) == 1
    # The stored pair is the old one, and it is NOT marked invalid: Bambu never refused it.
    assert await _stored(db_session, user_id) == ("acc", "ref", False)
