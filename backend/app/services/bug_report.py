"""Bug report service — posts to the bamdude.top relay which holds the GitHub PAT.

Self-hosters who don't want to rely on the public bamdude.top relay can override
``BUG_REPORT_RELAY_URL`` to point at their own relay (~50 LOC FastAPI service that
forwards to a maintainer-controlled GitHub PAT). The BamDude instance never holds
a PAT directly — that asymmetry is what keeps the feature safe to enable by
default on every install.
"""

import logging
import time

import httpx
from sqlalchemy import select

from backend.app.core.config import BUG_REPORT_RELAY_URL
from backend.app.core.database import async_session
from backend.app.models.bug_report import BugReport

logger = logging.getLogger(__name__)

# Process-local sliding window — survives until restart by design (max 5 reports/hour).
_rate_limit_window = 3600
_rate_limit_max = 5
_rate_limit_timestamps: list[float] = []


def _check_rate_limit() -> bool:
    """Check if rate limit allows a new report. Returns True if allowed."""
    now = time.time()
    _rate_limit_timestamps[:] = [t for t in _rate_limit_timestamps if now - t < _rate_limit_window]
    if len(_rate_limit_timestamps) >= _rate_limit_max:
        return False
    _rate_limit_timestamps.append(now)
    return True


async def submit_report(
    description: str,
    reporter_email: str | None,
    screenshot_base64: str | None,
    support_info: dict | None,
) -> dict:
    """Submit a bug report via the configured relay (default ``https://bamdude.top/api/bug-report``)."""
    if not _check_rate_limit():
        return {
            "success": False,
            "message": "Rate limit exceeded. Please try again later.",
            "issue_url": None,
            "issue_number": None,
        }

    if not BUG_REPORT_RELAY_URL:
        return {
            "success": False,
            "message": "Bug reporting is not configured. BUG_REPORT_RELAY_URL is not set.",
            "issue_url": None,
            "issue_number": None,
        }

    # ⚠️ Top level, not inside support_info. The relay reads ``payload.install_id``
    # and stores it on the report row; the copy the bundle carries in
    # ``support_info["app"]`` never reached that field, so every report to date
    # is stored with no install to correlate it against — which is the one thing
    # the id exists for. The relay's schema has accepted it all along, commented
    # "Added by newer clients"; we were the client that never started.
    #
    # Sent even when the reporter declined support info: the diagnostics are
    # what that opt-in covers, and an anonymous install id is not one of them.
    from backend.app.core.install_id import get_install_id

    payload: dict = {"description": description, "install_id": get_install_id()}
    if reporter_email:
        payload["reporter_email"] = reporter_email
    if screenshot_base64:
        payload["screenshot_base64"] = screenshot_base64
    if support_info:
        payload["support_info"] = support_info

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(BUG_REPORT_RELAY_URL, json=payload)
            if resp.status_code != 200:
                error_msg = f"Relay returned HTTP {resp.status_code}"
                logger.error("%s at %s", error_msg, BUG_REPORT_RELAY_URL)
                async with async_session() as db:
                    report = BugReport(
                        description=description,
                        reporter_email=reporter_email,
                        status="failed",
                        error_message=error_msg,
                    )
                    db.add(report)
                    await db.commit()
                return {
                    "success": False,
                    "message": "Bug report relay is not available. Please try again later.",
                    "issue_url": None,
                    "issue_number": None,
                }
            relay_data = resp.json()
    except Exception:
        logger.exception("Failed to reach bug report relay at %s", BUG_REPORT_RELAY_URL)
        async with async_session() as db:
            report = BugReport(
                description=description,
                reporter_email=reporter_email,
                status="failed",
                error_message="Failed to reach bug report relay",
            )
            db.add(report)
            await db.commit()

        return {
            "success": False,
            "message": "Failed to submit bug report. Please try again later.",
            "issue_url": None,
            "issue_number": None,
        }

    if not relay_data.get("success"):
        async with async_session() as db:
            report = BugReport(
                description=description,
                reporter_email=reporter_email,
                status="failed",
                error_message=relay_data.get("message", "Relay returned failure"),
            )
            db.add(report)
            await db.commit()

        return {
            "success": False,
            "message": relay_data.get("message", "Failed to create bug report."),
            "issue_url": None,
            "issue_number": None,
        }

    issue_number = relay_data["issue_number"]
    issue_url = relay_data["issue_url"]

    async with async_session() as db:
        report = BugReport(
            description=description,
            reporter_email=reporter_email,
            github_issue_number=issue_number,
            github_issue_url=issue_url,
            status="submitted",
            email_sent=True,
        )
        db.add(report)
        await db.commit()

    return {
        "success": True,
        "message": "Bug report submitted successfully!",
        "issue_url": issue_url,
        "issue_number": issue_number,
    }


def _status_from_issue(state: str, state_reason: str | None) -> str | None:
    """Map a GitHub issue state onto the local ``status`` column.

    ``unknown`` (the relay could not look this one up) maps to None — keep
    what we have rather than invent a state.
    """
    if state == "closed":
        return "not_planned" if state_reason == "not_planned" else "closed"
    if state == "open":
        return "open"
    return None


# States that will never change again — no point asking GitHub about them.
_TERMINAL_STATUSES = ("closed", "not_planned")


async def sync_report_statuses(db) -> bool:
    """Refresh local rows from the relay's GitHub status proxy. Best-effort.

    Returns False only when the relay could not be consulted at all — callers
    surface that as "statuses may be stale", never as an error: the local list
    is still worth showing. Deliberately via the relay and not GitHub directly:
    the PAT lives there, and 50 installs polling api.github.com anonymously
    would spend the per-IP rate limit on curiosity.
    """
    rows = (
        (
            await db.execute(
                select(BugReport)
                .where(BugReport.github_issue_number.is_not(None))
                .where(BugReport.status.not_in(_TERMINAL_STATUSES))
                .order_by(BugReport.id.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return True
    if not BUG_REPORT_RELAY_URL:
        return False

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{BUG_REPORT_RELAY_URL}/status",
                json={"issue_numbers": [r.github_issue_number for r in rows]},
            )
            if resp.status_code != 200:
                logger.warning("Bug-report status relay answered HTTP %s", resp.status_code)
                return False
            data = resp.json()
    except Exception:
        logger.warning("Bug-report status relay unreachable at %s", BUG_REPORT_RELAY_URL)
        return False

    if not data.get("success"):
        return False

    by_number = {s.get("issue_number"): s for s in data.get("statuses", []) if isinstance(s, dict)}
    changed = False
    for row in rows:
        status = by_number.get(row.github_issue_number)
        if not status:
            continue
        new = _status_from_issue(str(status.get("state", "")), status.get("state_reason"))
        if new and new != row.status:
            row.status = new
            changed = True
    if changed:
        await db.commit()
    return True
