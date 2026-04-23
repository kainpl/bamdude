"""Sliding-window rate limiting for auth-related endpoints (§18.5).

Uses the ``auth_rate_limit_events`` table (shipped in m012_mfa) as a simple
event-log: each failed attempt inserts a row, the check counts rows within
the last ``LOCKOUT_WINDOW`` minutes and rejects when above the threshold.

Event keys are buckets — typically the lowercased username for per-account
limits, and the resolved client IP (see ``_get_client_ip`` in routes/auth.py)
for per-IP limits. Both buckets run in parallel: a successful login clears
the bucket for that user, which is why bruteforcers see the IP bucket fill
up long before they find a valid username.

Known trade-off (L-2 in upstream security inventory): the SELECT + INSERT
aren't atomic, so two concurrent failed attempts can both observe the count
below the threshold and both proceed. The race window is microseconds and
we're measuring over minutes, so this is acceptable — a serialising lock
would add contention to a cold path for no real-world benefit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.auth_ephemeral import AuthRateLimitEvent, EventType

# Sliding-window lookback for all buckets below.
LOCKOUT_WINDOW = timedelta(minutes=15)

# Defaults — individual callers can pass their own ``max_attempts``.
MAX_2FA_ATTEMPTS = 5
MAX_LOGIN_ATTEMPTS_PER_USERNAME = 10
MAX_LOGIN_ATTEMPTS_PER_IP = 20
MAX_PASSWORD_RESET_PER_USERNAME = 3
MAX_PASSWORD_RESET_PER_IP = 10


async def check_rate_limit(
    db: AsyncSession,
    username: str,
    event_type: str = EventType.TWO_FA_ATTEMPT,
    max_attempts: int = MAX_2FA_ATTEMPTS,
) -> None:
    """Raise HTTP 429 if ``username`` has exceeded ``max_attempts`` recent events.

    ``username`` is lower-cased so case-variant spellings of the same login
    share a bucket. For IP-based checks the caller passes the IP string as
    ``username`` (the column is a plain VARCHAR, untyped).
    """
    username_key = username.lower()
    cutoff = datetime.now(timezone.utc) - LOCKOUT_WINDOW
    result = await db.execute(
        select(AuthRateLimitEvent).where(
            AuthRateLimitEvent.username == username_key,
            AuthRateLimitEvent.event_type == event_type,
            AuthRateLimitEvent.occurred_at > cutoff,
        )
    )
    if len(result.scalars().all()) >= max_attempts:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed attempts. Please try again later.",
        )


async def record_failed_attempt(
    db: AsyncSession,
    username: str,
    event_type: str = EventType.TWO_FA_ATTEMPT,
) -> None:
    """Record a failed attempt for rate-limiting purposes."""
    db.add(AuthRateLimitEvent(username=username.lower(), event_type=event_type))
    await db.commit()


async def clear_failed_attempts(
    db: AsyncSession,
    username: str,
    event_type: str = EventType.TWO_FA_ATTEMPT,
) -> None:
    """Delete all recorded failed attempts for a user on successful verification."""
    await db.execute(
        delete(AuthRateLimitEvent).where(
            AuthRateLimitEvent.username == username.lower(),
            AuthRateLimitEvent.event_type == event_type,
        )
    )
    await db.commit()
