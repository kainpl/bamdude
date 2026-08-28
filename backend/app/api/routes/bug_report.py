"""In-app bug report — collects debug logs + screenshot + support info, posts to relay."""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.routes.support import (
    _apply_log_level,
    _collect_support_info,
    _get_debug_setting,
    _get_recent_sanitized_logs,
    _set_debug_setting,
)
from backend.app.core.auth import RequirePermission
from backend.app.core.database import async_session, get_db
from backend.app.core.permissions import Permission
from backend.app.models.bug_report import BugReport
from backend.app.models.user import User
from backend.app.services.bug_report import submit_report, sync_report_statuses
from backend.app.services.printer_manager import printer_manager
from backend.app.services.support_projection import project_for_issue

router = APIRouter(prefix="/bug-report", tags=["bug-report"])
logger = logging.getLogger(__name__)


class BugReportRequest(BaseModel):
    description: str
    email: str | None = None
    screenshot_base64: str | None = None
    include_support_info: bool = True
    debug_logs: str | None = None


class BugReportResponse(BaseModel):
    success: bool
    message: str
    issue_url: str | None = None
    issue_number: int | None = None


class BugReportListItem(BaseModel):
    id: int
    description: str
    status: str
    github_issue_number: int | None = None
    github_issue_url: str | None = None
    created_at: datetime


class BugReportListResponse(BaseModel):
    # False = the relay could not be consulted; the list is still current
    # locally, only the GitHub-side statuses may be stale.
    synced: bool
    reports: list[BugReportListItem]


class StartLoggingResponse(BaseModel):
    started: bool
    was_debug: bool


class StopLoggingResponse(BaseModel):
    logs: str


@router.get("/reports", response_model=BugReportListResponse)
async def list_bug_reports(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_READ),
):
    """This install's submitted reports, statuses refreshed through the relay."""
    synced = await sync_report_statuses(db)
    rows = (await db.execute(select(BugReport).order_by(BugReport.id.desc()).limit(50))).scalars().all()
    return BugReportListResponse(
        synced=synced,
        reports=[
            BugReportListItem(
                id=r.id,
                # the list is a status board, not the report itself — the full
                # text already lives in the GitHub issue the row links to
                description=(r.description[:200] + "…") if len(r.description) > 200 else r.description,
                status=r.status,
                github_issue_number=r.github_issue_number,
                github_issue_url=r.github_issue_url,
                created_at=r.created_at,
            )
            for r in rows
        ],
    )


@router.post("/start-logging", response_model=StartLoggingResponse)
async def start_logging(
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
):
    """Enable debug logging and request a fresh status push from every connected printer."""
    async with async_session() as db:
        was_debug, _ = await _get_debug_setting(db)

    if not was_debug:
        async with async_session() as db:
            await _set_debug_setting(db, True)
        _apply_log_level(True)
        logger.info("Bug report: enabled debug logging")

    for printer_id in list(printer_manager._clients.keys()):
        try:
            printer_manager.request_status_update(printer_id)
        except Exception:
            logger.debug("Failed to push_all for printer %s", printer_id)

    return StartLoggingResponse(started=True, was_debug=was_debug)


@router.post("/stop-logging", response_model=StopLoggingResponse)
async def stop_logging(
    was_debug: bool = Query(default=False),
    _: User | None = RequirePermission(Permission.SETTINGS_READ),
):
    """Collect sanitized recent logs and restore the previous log level."""
    logs = await _get_recent_sanitized_logs()

    if not was_debug:
        async with async_session() as db:
            await _set_debug_setting(db, False)
        _apply_log_level(False)
        logger.info("Bug report: restored normal logging")

    return StopLoggingResponse(logs=logs)


@router.post("/submit", response_model=BugReportResponse)
async def submit_bug_report(
    report: BugReportRequest,
    _: User | None = RequirePermission(Permission.SETTINGS_READ),
):
    """Submit a bug report via the configured relay."""
    support_info = None
    if report.include_support_info:
        try:
            support_info = await _collect_support_info()
            if report.debug_logs:
                support_info["recent_logs"] = report.debug_logs
            # ⚠️ Only this path is budgeted. The relay prints the whole payload
            # into a GitHub issue body, which caps at 65 536 characters, and
            # exceeding it is a 422 that loses the entire report — shown to the
            # reporter as "Failed to create GitHub issue", which reads as the
            # relay being down. Two sections scale per printer, so a farm of
            # 13-19 printers already crossed that line.
            #
            # The downloaded ZIP takes the whole payload; it has no limit and is
            # where the maintainer goes for whatever did not fit here.
            support_info, trimmed = project_for_issue(support_info)
            if trimmed:
                support_info["_budget_notes"] = trimmed
                logger.info("Bug report pack trimmed to fit the issue budget: %s", "; ".join(trimmed))
        except Exception:
            logger.exception("Failed to collect support info for bug report")

    result = await submit_report(
        description=report.description,
        reporter_email=report.email,
        screenshot_base64=report.screenshot_base64,
        support_info=support_info,
    )
    return BugReportResponse(**result)
