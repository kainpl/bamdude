"""What the bug-report relay actually receives.

⚠️ The relay reads a TOP-LEVEL ``install_id`` and stores it on the report row —
its schema even comments the field as *"Added by newer clients; older clients
omit it"*. Carrying the id inside ``support_info["app"]``, which the bundle has
always done, does not reach that field. So every report to date is stored with
no install to correlate it against, which is the one thing the id exists for.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services import bug_report

pytestmark = pytest.mark.unit

INSTALL_ID = "11111111-2222-3333-4444-555555555555"


def _relay_ok():
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"success": True, "issue_number": 7, "issue_url": "https://example/7"}
    return resp


def _stub_transport(monkeypatch) -> AsyncMock:
    """Point the service at a relay that always accepts, and hand back the post."""
    monkeypatch.setattr(bug_report, "_check_rate_limit", lambda: True)
    monkeypatch.setattr(bug_report, "BUG_REPORT_RELAY_URL", "https://relay.invalid/api/bug-report")

    post = AsyncMock(return_value=_relay_ok())
    client = MagicMock()
    client.post = post
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(bug_report.httpx, "AsyncClient", lambda **kw: client)

    db = MagicMock(add=MagicMock(), commit=AsyncMock())
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=db)
    session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(bug_report, "async_session", lambda: session)
    # The row the service writes is not what these tests are about, and
    # constructing it would configure every ORM mapper for the sake of an
    # assertion about a dict.
    monkeypatch.setattr(bug_report, "BugReport", MagicMock())
    return post


async def test_the_payload_carries_a_top_level_install_id(monkeypatch):
    post = _stub_transport(monkeypatch)

    with patch("backend.app.core.install_id.get_install_id", return_value=INSTALL_ID):
        await bug_report.submit_report(
            description="it broke",
            reporter_email=None,
            screenshot_base64=None,
            support_info={"app": {"install_id": INSTALL_ID}},
        )

    assert post.await_args.kwargs["json"]["install_id"] == INSTALL_ID


async def test_the_id_is_sent_even_without_support_info(monkeypatch):
    """A reporter who unticked "include support info" is still an install we can
    correlate. An anonymous id is not part of that opt-in — the diagnostics are."""
    post = _stub_transport(monkeypatch)

    with patch("backend.app.core.install_id.get_install_id", return_value=INSTALL_ID):
        await bug_report.submit_report(
            description="it broke",
            reporter_email=None,
            screenshot_base64=None,
            support_info=None,
        )

    payload = post.await_args.kwargs["json"]
    assert payload["install_id"] == INSTALL_ID
    assert "support_info" not in payload
