"""A bug report's e-mail address goes to the relay and is not kept here.

Nothing on the installation read the stored copy (upstream 346b58ed8).
"""

from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from backend.app.migrations import m238_bug_report_emails_cleared as m238
from backend.app.models.bug_report import BugReport
from backend.app.services import bug_report as service


def relay(monkeypatch, *, status=200, body=None, raises=None):
    sent = {}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            sent["payload"] = json
            if raises:
                raise raises
            response = MagicMock(status_code=status)
            response.json.return_value = body or {"success": True, "issue_number": 7, "issue_url": "https://x/7"}
            return response

    monkeypatch.setattr(service.httpx, "AsyncClient", Client)
    return sent


@pytest.fixture
def stored(monkeypatch, test_engine):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(service, "async_session", factory)
    monkeypatch.setattr(service, "BUG_REPORT_RELAY_URL", "https://relay.invalid/report")
    service._rate_limit_timestamps.clear()

    async def rows():
        async with factory() as db:
            return list((await db.execute(select(BugReport))).scalars().all())

    return rows, factory


async def submit():
    return await service.submit_report("It broke", "me@example.org", None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        {},
        {"status": 502},
        {"raises": RuntimeError("no route")},
        {"body": {"success": False, "message": "refused"}},
    ],
    ids=["submitted", "relay-http-error", "relay-unreachable", "relay-refused"],
)
async def test_no_outcome_stores_the_address(monkeypatch, stored, outcome):
    rows, _factory = stored
    sent = relay(monkeypatch, **outcome)

    await submit()

    saved = await rows()
    assert len(saved) == 1
    assert saved[0].reporter_email is None
    assert sent["payload"]["reporter_email"] == "me@example.org"


@pytest.mark.asyncio
async def test_addresses_stored_before_are_cleared_once(stored):
    rows, factory = stored
    async with factory() as db:
        db.add(BugReport(description="old", reporter_email="old@example.org", status="submitted"))
        db.add(BugReport(description="anonymous", status="submitted"))
        await db.commit()

    await m238.seed(factory)
    await m238.seed(factory)

    saved = await rows()
    assert len(saved) == 2
    assert all(row.reporter_email is None for row in saved)
