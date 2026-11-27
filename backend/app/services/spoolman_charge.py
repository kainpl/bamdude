"""The accounting tail of a finished print owns one Spoolman source (C047 part 2, spec §C1).

Spec ``60-specs/c047-spool-id-never-reissued-spec.md`` (vault): «Допуск списання»
(S33, S35, S36, S40).

``report_usage`` (a completed print, with its runout correction) and
``cleanup_tracking`` (a failed or cancelled one: partial report, then runout
correction) each run inside ONE :func:`owning`. The ownership:

1. checks the charge gate synchronously — closed (a full restore, shutdown)
   means the tail writes NOTHING: no token, no ``use_spool``, no tracking
   delete; the usage is not recorded, and a log line says so;
2. takes the source guard (waiting: a source change never waits for a charge,
   it refuses), reads the three source rows;
3. checks the gate again and registers in ONE synchronous step, before the
   first write — the full restore's drain sees this owner from here on (S40);
4. as the owner: the mode and the address from that snapshot decide whether
   there is a Spoolman at all; the lazy source state is persisted when the
   address asks for it (the first token raises the generation, S42);
5. builds its OWN client on the snapshot's address (``/disconnect`` and
   ``/connect`` close the global one), health-checked once.

The token goes with the charges to ``record_print``; the ownership ends after
the tail's last write.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass

from sqlalchemy import select

from backend.app.core import database
from backend.app.services import spoolman_source

logger = logging.getLogger(__name__)


@dataclass(eq=False)
class ChargeContext:
    ownership: spoolman_source.ChargeOwnership
    enabled: bool
    url: str
    token: str | None
    _client: object | None = None
    _healthy: bool | None = None

    async def client(self):
        """This tail's own client, health-checked once; None when there is no reachable Spoolman."""
        if not self.enabled or not self.url:
            return None
        if self._client is None:
            from backend.app.services.spoolman import SpoolmanClient  # noqa: PLC0415

            self._client = SpoolmanClient(self.url)
        if self._healthy is None:
            self._healthy = await self._client.health_check()
        return self._client if self._healthy else None

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.close()


def _assert_clean(db) -> None:
    if db is None:
        return
    from backend.app.core.database import writer_reserved  # noqa: PLC0415

    if db.new or db.dirty or db.deleted or writer_reserved(db):
        raise RuntimeError("a Spoolman charge is admitted in a session with uncommitted writes")


async def _read_snapshot() -> dict[str, str | None]:
    from backend.app.models.settings import Settings  # noqa: PLC0415
    from backend.app.services.spoolman_usage_history import SOURCE_SETTING  # noqa: PLC0415

    keys = ("spoolman_enabled", "spoolman_url", SOURCE_SETTING)
    async with database.async_session() as db:
        rows = (await db.execute(select(Settings.key, Settings.value).where(Settings.key.in_(keys)))).all()
    return dict(rows)


async def admit(label: str, db=None) -> ChargeContext | None:
    """Steps 1–5 of the admission; None when the gate is closed."""
    _assert_clean(db)
    if not spoolman_source.charge_gate_open():
        return None
    async with spoolman_source.guard():
        snapshot = await _read_snapshot()
        ownership = spoolman_source.register_charge(label)
        if ownership is None:
            return None
        try:
            return await _as_owner(ownership, snapshot)
        except BaseException:
            spoolman_source.release_charge(ownership)
            raise


async def _as_owner(ownership, snapshot: dict) -> ChargeContext:
    import json  # noqa: PLC0415

    from backend.app.api.routes._spoolman_helpers import assert_safe_spoolman_url  # noqa: PLC0415
    from backend.app.services import spoolman_usage_history  # noqa: PLC0415

    enabled = (snapshot.get("spoolman_enabled") or "").strip().lower() == "true"
    url = (snapshot.get("spoolman_url") or "").strip()
    if enabled and url:
        try:
            assert_safe_spoolman_url(url)
        except ValueError as exc:
            logger.warning(
                "[SPOOLMAN] %s: the Spoolman address is not admitted (%s); nothing is charged", ownership.label, exc
            )
            enabled = False
    if not enabled or not url:
        return ChargeContext(ownership, False, url, None)
    try:
        data = json.loads(snapshot.get(spoolman_usage_history.SOURCE_SETTING) or "")
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data.get("token"):
        data = {}
    async with database.async_session() as db:
        resolved = await spoolman_usage_history.resolve_source(
            db, data=data, url=spoolman_usage_history.normalize_url(url)
        )
        token = await spoolman_usage_history.persist_source(db, resolved)
        await db.commit()
    return ChargeContext(ownership, True, url, token)


@contextlib.asynccontextmanager
async def owning(label: str, db=None):
    """One accounting tail's ownership; yields None when the charge gate is closed."""
    context = await admit(label, db)
    if context is None:
        logger.warning("[SPOOLMAN] %s: usage not recorded — a restore or shutdown closed Spoolman accounting", label)
    try:
        yield context
    finally:
        if context is not None:
            try:
                await context.close()
            finally:
                spoolman_source.release_charge(context.ownership)
