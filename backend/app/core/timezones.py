"""Which day boundary applies, and whose.

Two different questions, deliberately answered by two different functions:

- **What the server does on its own schedule** — nightly backups, digests, stock
  forecasts, hourly snapshots — happens in the *server's* timezone. There is no
  client involved, so there is nothing to ask.
- **What a person is looking at right now** — "today's energy", a date range in
  statistics — is formatted for the timezone of the browser asking. "Today" on
  someone's screen should mean their today.

Getting this backwards is not a rounding error: a farm in Europe/Kyiv reading
"today" computed from UTC midnight sees the first three hours of every day
attributed to yesterday.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Request

logger = logging.getLogger(__name__)

# Sent by the frontend on every API call (``api/client.ts``). Named for what it
# is rather than the generic "X-Timezone", because the server has one too and
# confusing them is the whole hazard this module exists to prevent.
CLIENT_TIMEZONE_HEADER = "X-Client-Timezone"


def server_timezone() -> tzinfo:
    """The timezone the deployment runs in, from the ``TZ`` env var.

    ``docker-compose.yml`` sets it (defaulting to Europe/Kyiv) and the support
    bundle reports it, so on a normal install this is the farm's own timezone.

    Falls back to stdlib ``timezone.utc`` rather than ``ZoneInfo("UTC")``: a host
    without an IANA database — Windows with no ``tzdata`` wheel — must still
    resolve something instead of raising.
    """
    tz_name = os.environ.get("TZ", "").strip()
    if not tz_name:
        return timezone.utc
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ModuleNotFoundError, KeyError):
        logger.warning("Unrecognised TZ env value %r — falling back to UTC", tz_name)
        return timezone.utc


def client_timezone(request: Request) -> tzinfo:
    """The browser's timezone, or the server's when there isn't one.

    Falls back to the *server's* timezone rather than UTC on purpose. The callers
    without this header are not in some neutral place — they are the Telegram
    bot, API keys, webhooks and Prometheus, all of which belong to this
    deployment. Answering them in UTC would put a scripted "today" three hours
    away from the same question asked in the UI.

    The value is untrusted input going into ``ZoneInfo``, which raises on
    anything it does not recognise, so an unusable header degrades to the
    fallback instead of 500-ing a statistics page.
    """
    raw = (request.headers.get(CLIENT_TIMEZONE_HEADER) or "").strip()
    if not raw:
        return server_timezone()
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ModuleNotFoundError, KeyError, ValueError):
        logger.debug("Ignoring unusable %s header %r", CLIENT_TIMEZONE_HEADER, raw)
        return server_timezone()


def day_bounds(day: date, tz: tzinfo) -> tuple[datetime, datetime]:
    """The UTC instants a calendar day starts and ends at, in ``tz``.

    Returned in UTC because that is what the stored timestamps are, so callers
    compare like with like and never mix an aware local value into a query
    against naive UTC columns.

    The end is the start of the following day, i.e. exclusive. Using
    ``time.max`` would drop the final 999 microseconds — invisible until a row
    lands in them and someone spends an afternoon on it.
    """
    start_local = datetime.combine(day, time.min, tzinfo=tz)
    end_local = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def start_of_today(tz: tzinfo) -> datetime:
    """The UTC instant the current day began in ``tz``."""
    now_local = datetime.now(tz)
    return datetime.combine(now_local.date(), time.min, tzinfo=tz).astimezone(timezone.utc)
