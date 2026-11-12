"""The existing SSE writer rechecks each page; progress only follows commit."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from backend.app.api.routes import inventory
from backend.app.core.auth_lifetime import stage_revoke_session
from backend.app.core.auth_runtime import auth_runtime
from backend.app.models.color_catalog import ColorCatalogEntry
from backend.tests.unit.services.test_g01_d_live_authority import issue, live  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def provider(monkeypatch, before_page=None):
    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            pass

        async def get(self, url, params):
            page = params["page"]
            if before_page:
                await before_page(page)
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={
                    "count": 2,
                    "next": page == 1,
                    "results": [{"manufacturer": {"name": "test"}, "color_name": str(page), "hex_color": "123456"}],
                },
            )

    monkeypatch.setattr(inventory, "httpx", SimpleNamespace(AsyncClient=Client, HTTPError=httpx.HTTPError))


async def response_for(issued):
    request = Request(
        {"type": "http", "headers": [(b"authorization", f"Bearer {issued.access_token}".encode())], "state": {}}
    )
    return await inventory.sync_from_filamentcolors(request, None)


async def rows(factory):
    async with factory() as db:
        return await db.scalar(select(func.count()).select_from(ColorCatalogEntry))


async def test_next_page_rechecks_revoked_actor_and_never_reports_complete(live, monkeypatch):
    _, factory = live
    issued, _ = await issue(factory)

    async def revoke(page):
        if page == 2:
            async with factory() as db:
                await stage_revoke_session(db, issued.capture)
                await db.commit()

    provider(monkeypatch, revoke)
    response = await response_for(issued)
    events = [json.loads(chunk.removeprefix("data: ")) async for chunk in response.body_iterator]
    assert [(item["type"], item.get("added")) for item in events] == [("progress", 1), ("error", None)]
    assert await rows(factory) == 1
    assert not auth_runtime._transactions


async def test_failed_page_commit_never_reports_added_or_complete(live, monkeypatch):
    _, factory = live
    issued, _ = await issue(factory)
    provider(monkeypatch)
    response = await response_for(issued)
    original = AsyncSession.commit

    async def fail(db):
        if any(isinstance(row, ColorCatalogEntry) for row in db.new):
            raise RuntimeError("provider-secret-must-not-escape")
        await original(db)

    monkeypatch.setattr(AsyncSession, "commit", fail)
    events = [json.loads(chunk.removeprefix("data: ")) async for chunk in response.body_iterator]
    assert events == [{"type": "error", "error": "Unexpected error during sync"}]
    assert await rows(factory) == 0
    assert not auth_runtime._transactions


async def test_cancelled_page_transaction_remains_restore_owned_until_actual_close(live, monkeypatch):
    _, factory = live
    issued, _ = await issue(factory)
    provider(monkeypatch)
    response = await response_for(issued)
    entered = asyncio.Event()
    never = asyncio.Event()
    original = AsyncSession.commit

    async def hold(db):
        if any(isinstance(row, ColorCatalogEntry) for row in db.new):
            entered.set()
            await never.wait()
        await original(db)

    monkeypatch.setattr(AsyncSession, "commit", hold)
    task = asyncio.create_task(anext(response.body_iterator))
    await asyncio.wait_for(entered.wait(), 2)
    assert auth_runtime._transactions
    assert not await auth_runtime.quiesce_for_restore(timeout=0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await auth_runtime.quiesce_for_restore(timeout=2)
    assert await rows(factory) == 0
