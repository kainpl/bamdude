"""Bug-report status sync: local rows follow the GitHub issue through the relay.

The ``bug_reports`` table was a write-only forensic trail (2026-08-23 dead-code
audit); on request it gained a read surface, and this sync keeps the ``status``
column honest — via the relay's ``POST /status`` proxy, never GitHub directly:
the PAT lives in the relay, and an install must not need one of its own.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.bug_report import BugReport
from backend.app.services.bug_report import sync_report_statuses


async def _row(db_session, issue=100, status="submitted"):
    r = BugReport(description="d", github_issue_number=issue, github_issue_url=f"https://x/{issue}", status=status)
    db_session.add(r)
    await db_session.commit()
    await db_session.refresh(r)
    return r


def _relay(payload):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=resp)
    return patch("backend.app.services.bug_report.httpx.AsyncClient", return_value=client)


@pytest.mark.asyncio
async def test_a_closed_issue_closes_the_row(db_session):
    row = await _row(db_session, issue=100)
    payload = {"success": True, "statuses": [{"issue_number": 100, "state": "closed", "state_reason": "completed"}]}

    with _relay(payload):
        assert await sync_report_statuses(db_session) is True

    await db_session.refresh(row)
    assert row.status == "closed"


@pytest.mark.asyncio
async def test_not_planned_is_its_own_terminal_state(db_session):
    row = await _row(db_session, issue=101)
    payload = {"success": True, "statuses": [{"issue_number": 101, "state": "closed", "state_reason": "not_planned"}]}

    with _relay(payload):
        await sync_report_statuses(db_session)

    await db_session.refresh(row)
    assert row.status == "not_planned"


@pytest.mark.asyncio
async def test_an_open_issue_marks_the_row_open(db_session):
    row = await _row(db_session, issue=102)
    payload = {"success": True, "statuses": [{"issue_number": 102, "state": "open", "state_reason": None}]}

    with _relay(payload):
        await sync_report_statuses(db_session)

    await db_session.refresh(row)
    assert row.status == "open"


@pytest.mark.asyncio
async def test_an_unknown_lookup_keeps_the_current_status(db_session):
    row = await _row(db_session, issue=103)
    payload = {"success": True, "statuses": [{"issue_number": 103, "state": "unknown", "state_reason": None}]}

    with _relay(payload):
        assert await sync_report_statuses(db_session) is True

    await db_session.refresh(row)
    assert row.status == "submitted"


@pytest.mark.asyncio
async def test_terminal_rows_are_not_asked_about_again(db_session):
    await _row(db_session, issue=104, status="closed")

    client = MagicMock()
    with patch("backend.app.services.bug_report.httpx.AsyncClient", return_value=client):
        assert await sync_report_statuses(db_session) is True

    client.post.assert_not_called()


@pytest.mark.asyncio
async def test_a_relay_outage_is_reported_and_changes_nothing(db_session):
    row = await _row(db_session, issue=105)

    client = MagicMock()
    client.__aenter__ = AsyncMock(side_effect=OSError("down"))
    client.__aexit__ = AsyncMock(return_value=False)
    with patch("backend.app.services.bug_report.httpx.AsyncClient", return_value=client):
        assert await sync_report_statuses(db_session) is False

    await db_session.refresh(row)
    assert row.status == "submitted"


@pytest.mark.asyncio
async def test_rows_that_never_reached_github_are_left_alone(db_session):
    r = BugReport(description="d", status="failed")
    db_session.add(r)
    await db_session.commit()

    client = MagicMock()
    with patch("backend.app.services.bug_report.httpx.AsyncClient", return_value=client):
        assert await sync_report_statuses(db_session) is True

    client.post.assert_not_called()
    rows = (await db_session.execute(select(BugReport))).scalars().all()
    assert [x.status for x in rows] == ["failed"]
