"""Owned, post-admission delivery of durable credential-retirement facts.

Informational failures never alter auth admission. SQL and cleanup stay owned
after a timeout; restore refuses replacement until this owner proves drainage.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime

from sqlalchemy import select, text, update

from backend.app.core import database
from backend.app.models.credential_rollout import CredentialRolloutEpisode
from backend.app.services.notification_inbox import dispatch_staged, stage_admin_event
from backend.app.services.notification_service import InternalTemplateNotReady, render_internal_event
from backend.app.services.preview_artifacts import owned

logger = logging.getLogger(__name__)
CONTRACT_KEY = "g01-credential-contract-v1"
EVENT = "credentials_reissue_required"


class CleanupUnproven(Exception):
    """The old checkout cannot safely be replaced or followed by another attempt."""


def _remaining(deadline: float) -> float:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError()
    return remaining


@asynccontextmanager
async def _session(deadline: float):
    """Attempt-local SQL budgets; cleanup is outside the execution deadline."""
    db = None
    connection = None
    old_busy = None
    try:
        # Keep an explicit checkout through commit and option reset. A session
        # bound directly to the engine returns its connection at commit, which
        # would leak our temporary SQLite busy_timeout into the pool.
        connection = await database.async_session.kw["bind"].connect()
        db = database.async_session(bind=connection)
        milliseconds = max(1, int(_remaining(deadline) * 1000))
        if db.get_bind().dialect.name == "sqlite":
            old_busy = (await connection.exec_driver_sql("PRAGMA busy_timeout")).scalar_one()
            await connection.exec_driver_sql(f"PRAGMA busy_timeout = {milliseconds}")
            await connection.commit()
        else:
            await db.execute(text("SELECT set_config('statement_timeout', :v, true)"), {"v": str(milliseconds)})
            await db.execute(text("SELECT set_config('lock_timeout', :v, true)"), {"v": str(milliseconds)})
        yield db
    finally:

        async def cleanup():
            try:
                if db is not None:
                    await db.rollback()
                # Rollback/commit can release the session connection. Restore
                # the connection-local SQLite option BEFORE returning it.
                if old_busy is not None and connection is not None and not connection.closed:
                    try:
                        await connection.exec_driver_sql(f"PRAGMA busy_timeout = {int(old_busy)}")
                        await connection.commit()
                    except BaseException:
                        await connection.invalidate()
                        raise
            finally:
                try:
                    if db is not None:
                        await db.close()
                finally:
                    if connection is not None:
                        await connection.close()

        async def prove_cleanup():
            try:
                await cleanup()
            except BaseException:
                # A failed rollback/reset/close is recoverable only if actual
                # driver invalidation + closure can still be proven.
                if connection is not None:
                    try:
                        await connection.invalidate()
                        await connection.close()
                    except BaseException:
                        raise CleanupUnproven() from None
                raise

        cleanup_task = asyncio.create_task(prove_cleanup(), name="credential-rollout-sql-cleanup")
        try:
            await owned(cleanup_task)
        except asyncio.CancelledError:
            # Preserve the stronger refusal even if the original attempt was
            # already cancelled when its cleanup lost proof.
            if not cleanup_task.cancelled() and isinstance(cleanup_task.exception(), CleanupUnproven):
                raise CleanupUnproven() from None
            raise


class CredentialRolloutRuntime:
    def __init__(self, *, attempt_seconds: float = 10, readiness_seconds: float = 5, retry_seconds: float = 60):
        self.attempt_seconds = attempt_seconds
        self.readiness_seconds = readiness_seconds
        self.retry_seconds = retry_seconds
        self._loop_task: asyncio.Task | None = None
        self._attempt_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._phase = "stopped"
        self._cleanup_unproven = False

    @property
    def phase(self) -> str:
        return self._phase

    def start(self, ready_probe: Callable[[], bool]) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        if self._cleanup_unproven or (self._attempt_task is not None and not self._attempt_task.done()):
            raise RuntimeError("Credential rollout work has not drained")
        self._stop = asyncio.Event()
        self._phase = "awaiting_admission"
        self._loop_task = asyncio.create_task(self._run(ready_probe), name="credential-rollout")

    async def _pause(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _run(self, ready_probe: Callable[[], bool]) -> None:
        try:
            while not self._stop.is_set() and not ready_probe():
                await self._pause(0.05)
            while not self._stop.is_set():
                self._attempt_task = asyncio.create_task(self._attempt(), name="credential-rollout-attempt")
                try:
                    pending = await asyncio.shield(self._attempt_task)
                except asyncio.CancelledError:
                    if self._stop.is_set():
                        return
                    raise
                if self._cleanup_unproven:
                    self._phase = "cleanup_unproven"
                    return
                if not pending:
                    self._phase = "complete"
                    return
                self._phase = "pending"
                # Actual attempt cleanup has ended before this cadence starts.
                await self._pause(self.retry_seconds)
        finally:
            if self._stop.is_set() and not self._cleanup_unproven:
                self._phase = "stopped"

    async def _attempt(self) -> bool:
        self._phase = "attempting"
        deadline = asyncio.get_running_loop().time() + self.attempt_seconds
        try:
            async with asyncio.timeout(self.attempt_seconds):
                return await self._reconcile(deadline)
        except asyncio.CancelledError:
            raise
        except CleanupUnproven:
            self._cleanup_unproven = True
            self._phase = "cleanup_unproven"
            logger.warning("Credential rollout cleanup unproven; retries stopped")
            return True
        except Exception:
            # Deliberately no peer/SQL exception, values, rows or traceback.
            logger.warning("Credential rollout remains pending; reconcile did not complete")
            return True

    async def _reconcile(self, deadline: float) -> bool:
        readiness_deadline = min(deadline, asyncio.get_running_loop().time() + self.readiness_seconds)
        while True:
            async with asyncio.timeout_at(readiness_deadline):
                async with _session(readiness_deadline) as db:
                    episode = (
                        await db.execute(
                            select(CredentialRolloutEpisode).where(
                                CredentialRolloutEpisode.contract_key == CONTRACT_KEY
                            )
                        )
                    ).scalar_one_or_none()
                    if episode is None or episode.delivered_at is not None:
                        return False
                    variables = _counts(episode)
                    try:
                        await render_internal_event(db, EVENT, variables)
                        break
                    except InternalTemplateNotReady:
                        pass
            _remaining(readiness_deadline)
            await asyncio.sleep(min(0.1, _remaining(readiness_deadline)))

        async with _session(deadline) as db:
            # SQLite's write reservation precedes authoritative episode/account
            # selection; PG locks the episode, then the account guard does its order.
            if db.get_bind().dialect.name == "sqlite":
                await db.execute(
                    update(CredentialRolloutEpisode)
                    .where(CredentialRolloutEpisode.contract_key == CONTRACT_KEY)
                    .values(delivered_at=CredentialRolloutEpisode.delivered_at)
                )
            episode = (
                await db.execute(
                    select(CredentialRolloutEpisode)
                    .where(CredentialRolloutEpisode.contract_key == CONTRACT_KEY)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if episode is None or episode.delivered_at is not None:
                return False
            title, message = await render_internal_event(db, EVENT, _counts(episode))
            batch = await stage_admin_event(
                db, event_type=EVENT, episode_id=episode.id, title=title, message=message, extra_data=_counts(episode)
            )
            episode.delivered_at = datetime.utcnow()
            _remaining(deadline)
            # Lost reply is deliberately NOT compensated with a pending write.
            # Next owned attempt reconciles the durable marker and unique rows.
            await db.commit()
        await dispatch_staged(batch, seconds=min(1.0, _remaining(deadline)))
        return False

    async def quiesce_for_restore(self, seconds: float) -> bool:
        self._stop.set()  # close scheduling before the first await
        if self._cleanup_unproven:
            return False
        if self._attempt_task is not None and not self._attempt_task.done():
            self._attempt_task.cancel()
        tasks = [t for t in (self._loop_task, self._attempt_task) if t is not None and not t.done()]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=max(0, seconds))
            return not pending and not self._cleanup_unproven
        return True

    async def shutdown(self, seconds: float = 10) -> bool:
        drained = await self.quiesce_for_restore(seconds)
        if not drained:
            logger.warning("Credential rollout shutdown drain unproven; work remains owned")
        return drained


def _counts(episode: CredentialRolloutEpisode) -> dict:
    return {
        "ownerless_api_key_count": episode.ownerless_api_key_count,
        "owned_api_key_count": episode.owned_api_key_count,
        "long_lived_token_count": episode.long_lived_token_count,
    }


async def status(db) -> dict:
    """Safe scalar projection; caller supplies the authenticated/authorized session."""
    with db.no_autoflush:
        episode = (
            await db.execute(
                select(CredentialRolloutEpisode).where(CredentialRolloutEpisode.contract_key == CONTRACT_KEY)
            )
        ).scalar_one_or_none()
    return {
        "contract_key": CONTRACT_KEY,
        "episode_id": episode.id if episode else None,
        **(
            _counts(episode)
            if episode
            else {"ownerless_api_key_count": 0, "owned_api_key_count": 0, "long_lived_token_count": 0}
        ),
        "created_at": episode.created_at if episode else None,
        "delivered_at": episode.delivered_at if episode else None,
        "notification_state": "none" if episode is None else "delivered" if episode.delivered_at else "pending",
        "runtime_phase": credential_rollout.phase,
    }


credential_rollout = CredentialRolloutRuntime()
