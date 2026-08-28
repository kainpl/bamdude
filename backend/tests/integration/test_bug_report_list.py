"""GET /bug-report/reports — the read surface of the local bug_reports trail."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from backend.app.models.bug_report import BugReport


@pytest.mark.asyncio
@pytest.mark.integration
async def test_reports_come_back_newest_first_with_the_sync_flag(async_client: AsyncClient, db_session):
    db_session.add(
        BugReport(description="first", status="submitted", github_issue_number=1, github_issue_url="https://x/1")
    )
    db_session.add(BugReport(description="second", status="failed"))
    await db_session.commit()

    with patch("backend.app.api.routes.bug_report.sync_report_statuses", new=AsyncMock(return_value=False)) as sync:
        resp = await async_client.get("/api/v1/bug-report/reports")

    assert resp.status_code == 200
    body = resp.json()
    assert body["synced"] is False
    assert [r["description"] for r in body["reports"]] == ["second", "first"]
    assert body["reports"][1]["github_issue_url"] == "https://x/1"
    sync.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_long_description_is_trimmed_for_the_list(async_client: AsyncClient, db_session):
    db_session.add(BugReport(description="x" * 500, status="submitted"))
    await db_session.commit()

    with patch("backend.app.api.routes.bug_report.sync_report_statuses", new=AsyncMock(return_value=True)):
        resp = await async_client.get("/api/v1/bug-report/reports")

    assert resp.json()["synced"] is True
    assert len(resp.json()["reports"][0]["description"]) == 201
