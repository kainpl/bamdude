"""Anonymized opt-out telemetry sender.

Once a day (after a short post-startup delay, with jitter) this posts an
anonymized snapshot — version, platform, aggregate counts, feature flags, daily
usage — keyed by the random install id, to ``TELEMETRY_RELAY_URL``. It NEVER
sends names, serials, IPs, file paths, settings values or credentials.

Opt-out: on by default; skipped when the ``telemetry_enabled`` setting is
explicitly false, when ``TELEMETRY_DISABLED`` is set, or when no relay URL /
install id is available. All network/DB errors are swallowed.
"""

import asyncio
import logging
import os
import platform
import random
from datetime import date, datetime, time, timezone

import httpx
from sqlalchemy import func, select

from backend.app.core.config import APP_VERSION, TELEMETRY_DISABLED, TELEMETRY_RELAY_URL
from backend.app.core.database import async_session
from backend.app.core.install_id import get_install_id

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_INITIAL_DELAY_SECONDS = 300  # first ping ~5 min after startup
_INTERVAL_SECONDS = 24 * 60 * 60


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _in_docker() -> bool:
    try:
        return os.path.exists("/.dockerenv")
    except OSError:
        return False


def _channel() -> str:
    return "pre" if any(c.isalpha() for c in APP_VERSION) else "stable"


async def _count(db, model, *where) -> int:
    stmt = select(func.count()).select_from(model)
    if where:
        stmt = stmt.where(*where)
    return int(await db.scalar(stmt) or 0)


async def _features(db) -> dict[str, bool]:
    from backend.app.api.routes.settings import get_setting
    from backend.app.models.git_backup import GitBackupConfig
    from backend.app.models.notification import NotificationProvider
    from backend.app.models.oidc_provider import OIDCProvider

    feats: dict[str, bool] = {}

    async def safe(key: str, coro) -> None:
        try:
            feats[key] = bool(await coro)
        except Exception as e:  # noqa: BLE001 - feature probe is best-effort
            logger.debug("telemetry feature probe %s failed: %s", key, e)

    await safe("spoolman", _setting_true(db, get_setting, "spoolman_enabled"))
    await safe("obico", _setting_true(db, get_setting, "obico_enabled"))
    await safe("slicer_api", _setting_true(db, get_setting, "use_slicer_api"))
    await safe(
        "telegram",
        _gt0(
            db,
            NotificationProvider,
            NotificationProvider.provider_type == "telegram",
            NotificationProvider.enabled.is_(True),
        ),
    )
    await safe("oidc", _gt0(db, OIDCProvider))
    await safe("git_backup", _gt0(db, GitBackupConfig))
    return feats


async def _setting_true(db, get_setting, key: str) -> bool:
    return _truthy(await get_setting(db, key))


async def _gt0(db, model, *where) -> bool:
    return (await _count(db, model, *where)) > 0


async def _build_payload(db) -> dict | None:
    install_id = get_install_id()
    if not install_id:
        return None

    from backend.app.models.archive import PrintArchive
    from backend.app.models.printer import Printer
    from backend.app.models.project import Project
    from backend.app.models.smart_plug import SmartPlug
    from backend.app.models.spool import Spool

    failure_states = ["failed", "aborted", "cancelled", "stopped"]
    start_of_today = datetime.combine(date.today(), time.min, tzinfo=timezone.utc)

    counts = {
        "archives": await _count(db, PrintArchive),
        "archives_completed": await _count(db, PrintArchive, PrintArchive.status == "completed"),
        "printers": await _count(db, Printer),
        "spools": await _count(db, Spool),
        "projects": await _count(db, Project),
        "smart_plugs": await _count(db, SmartPlug),
    }

    model_rows = await db.execute(select(Printer.model).where(Printer.model.isnot(None)).distinct())
    printer_models = sorted({m for (m,) in model_rows.all() if m})

    usage = {
        "prints_completed": await _count(
            db, PrintArchive, PrintArchive.status == "completed", PrintArchive.created_at >= start_of_today
        ),
        "prints_failed": await _count(
            db, PrintArchive, PrintArchive.status.in_(failure_states), PrintArchive.created_at >= start_of_today
        ),
    }

    return {
        "install_id": install_id,
        "version": APP_VERSION,
        "channel": _channel(),
        "platform": platform.system(),
        "platform_release": platform.release(),
        "arch": platform.machine(),
        "python_version": platform.python_version(),
        "docker": _in_docker(),
        "snapshot_date": date.today().isoformat(),
        "counts": counts,
        "printer_models": printer_models,
        "features": await _features(db),
        "usage": usage,
    }


async def _is_enabled(db) -> bool:
    if TELEMETRY_DISABLED or not TELEMETRY_RELAY_URL:
        return False
    from backend.app.api.routes.settings import get_setting

    value = await get_setting(db, "telemetry_enabled")
    # Opt-out: default ON; only an explicit false turns it off.
    return value is None or _truthy(value)


async def send_telemetry_once() -> bool:
    """Build + send one snapshot. Returns False when skipped/failed (never raises)."""
    try:
        async with async_session() as db:
            if not await _is_enabled(db):
                return False
            payload = await _build_payload(db)
        if not payload:
            return False
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(TELEMETRY_RELAY_URL, json=payload)
        return True
    except Exception as e:  # noqa: BLE001 - telemetry must never disrupt the app
        logger.debug("telemetry send failed: %s", e)
        return False


async def forget_telemetry() -> None:
    """Ask the relay to erase this install's data (fired when the user opts out)."""
    install_id = get_install_id()
    if TELEMETRY_DISABLED or not TELEMETRY_RELAY_URL or not install_id:
        return
    try:
        url = f"{TELEMETRY_RELAY_URL.rstrip('/')}/forget"
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(url, json={"install_id": install_id})
    except Exception as e:  # noqa: BLE001
        logger.debug("telemetry forget failed: %s", e)


async def _loop() -> None:
    await asyncio.sleep(_INITIAL_DELAY_SECONDS + random.uniform(0, 60))
    while True:
        try:
            await send_telemetry_once()
        except asyncio.CancelledError:
            break
        except Exception as e:  # noqa: BLE001
            logger.debug("telemetry loop error: %s", e)
        await asyncio.sleep(_INTERVAL_SECONDS + random.uniform(0, 3600))


def start_telemetry() -> None:
    global _task
    if TELEMETRY_DISABLED:
        logger.info("Telemetry disabled via TELEMETRY_DISABLED")
        return
    if _task is None:
        _task = asyncio.create_task(_loop())
        logger.info("Telemetry scheduler started (opt-out; daily)")


def stop_telemetry() -> None:
    global _task
    if _task:
        _task.cancel()
        _task = None
        logger.info("Telemetry scheduler stopped")
