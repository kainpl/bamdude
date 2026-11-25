"""The one door that deletes a Spoolman spool (C047 part 2, spec §C2, §C5, §C7).

A Spoolman spool that BamDude's history says was printed with is archived, not
deleted — the same rule as the built-in inventory (owner, 25.11). Single and
bulk delete both come here.

The order of one delete (spec §C2): every proof is taken BEFORE what it guards,
and one synchronous admission (:func:`_try_admit`) compares all three.

1. permission (the route), mode, a first look at the runtime's admission;
2. early local refusals with no HTTP (a feeding slot, an open print event, an
   accounting window) — economy only;
3. the source: guard free → G0 → one SELECT of the three rows (S0) → still G0;
   the mode and the address from S0 are the final word (S28);
4. the number's fence revision F0 — before health and GET (S21);
5. health and one GET: ``registered`` and the spool's identifiers;
6. activity first (A0, nothing open), then short local reads: history of the
   current source (``belongs_to``), slots, tags of every open print's trays
   (S26, S34), open journal events;
7. :func:`_try_admit` — synchronous, after the last await: admission open, G0,
   A0, live printer state of the slots read, F0 — then the lease and the task;
8. the task (:func:`_delete_and_clean`): DELETE with S0's own client, then the
   local cleanup under the source guard, or an outcome said as it is.

The task belongs to the runtime from the moment it is registered: a cancelled
request never stops it, and a full restore waits for it (or refuses).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

from fastapi import HTTPException
from sqlalchemy import delete, select

from backend.app.core import database
from backend.app.services import spoolman_activity, spoolman_binding_fence, spoolman_source

logger = logging.getLogger(__name__)


# --- refusals -----------------------------------------------------------------

# The sentences of this door's coded refusals — one map, translated at the API boundary
# (registered in ``scripts/api_error_catalog.py``); the frontend reacts to the code.
SPOOLMAN_DELETE_MESSAGES = {
    "spoolman_source_changed": "Spoolman settings or its history are changing right now. Try again",
    "spool_state_changed": "The spool changed while it was being checked. Try again",
    "print_closing": "A print is finishing and may still charge this spool. Try again in a moment",
    "spool_in_use": "This spool has usage history or is in use and cannot be deleted; archive it instead",
    "spool_feeds_print": "This spool is in a slot a running print feeds from; it can be deleted when the print ends",
    "spoolman_outcome_unknown": "Spoolman did not confirm the delete; check the spool in Spoolman",
}


def _coded(status: int, code: str, **extra) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": SPOOLMAN_DELETE_MESSAGES[code], **extra})


def _source_changed() -> HTTPException:
    return _coded(409, "spoolman_source_changed")


def _state_changed() -> HTTPException:
    return _coded(409, "spool_state_changed")


def _print_closing() -> HTTPException:
    return _coded(409, "print_closing")


def _in_use(reasons: list[str]) -> HTTPException:
    return _coded(409, "spool_in_use", reasons=reasons)


def _feeds_print() -> HTTPException:
    return _coded(409, "spool_feeds_print")


def _paused() -> HTTPException:
    return HTTPException(status_code=503, detail="Spoolman deletes are paused while a restore runs")


# --- the runtime ----------------------------------------------------------------


class DeleteRuntime:
    """Owner of every admitted delete task (spec §C5)."""

    def __init__(self) -> None:
        self.admitting = True
        self.tasks: set[asyncio.Task] = set()

    def open_admission(self) -> None:
        self.admitting = True
        spoolman_source.open_charge_admission()

    def close_admission(self) -> None:
        """Synchronous: closes the delete AND the charge gate (S35)."""
        self.admitting = False
        spoolman_source.close_charge_admission()

    def _done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled():
            task.exception()

    def register(self, coroutine) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self._done)
        return task

    async def quiesce_for_restore(self, timeout: float = 30.0) -> bool:
        """Close both gates, then wait — outside the source guard — for delete tasks and charge owners."""
        self.close_admission()
        deadline = time.monotonic() + timeout
        if self.tasks:
            _done, pending = await asyncio.wait(tuple(self.tasks), timeout=timeout)
            if pending:
                return False
        left = max(0.0, deadline - time.monotonic())
        return await spoolman_source.quiesce_charges(left)


delete_runtime = DeleteRuntime()


# --- the result -----------------------------------------------------------------


@dataclass
class DeleteOutcome:
    """A confirmed remote delete, with the local cleanup's warning if any (spec §C7)."""

    warning: str | None = None
    slots: list[tuple[int, int, int]] = field(default_factory=list)

    def as_response(self) -> dict:
        body: dict = {"status": "deleted"}
        if self.warning:
            warning: dict = {"code": self.warning}
            if self.slots:
                warning["slots"] = [{"printer_id": p, "ams_id": a, "tray_id": t} for p, a, t in self.slots]
            body["warning"] = warning
        return body


# --- step helpers -------------------------------------------------------------


@dataclass
class _Source:
    generation: int
    enabled: bool
    url: str
    token: str | None


async def _read_source_snapshot() -> tuple[dict, dict]:
    from backend.app.models.settings import Settings  # noqa: PLC0415
    from backend.app.services.spoolman_usage_history import SOURCE_SETTING  # noqa: PLC0415

    keys = ("spoolman_enabled", "spoolman_url", SOURCE_SETTING)
    async with database.async_session() as db:
        rows = dict((await db.execute(select(Settings.key, Settings.value).where(Settings.key.in_(keys)))).all())
    try:
        data = json.loads(rows.get(SOURCE_SETTING) or "")
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data.get("token"):
        data = {}
    return rows, data


def _pure_token(data: dict, url: str) -> str | None:
    """The token S0 names without any further read (spec §C1): the record's, else the known one."""
    from backend.app.services.spoolman_usage_history import _known  # noqa: PLC0415

    if not data:
        return None
    if not url or url == data.get("url"):
        return data["token"]
    return _known(data).get(url)


async def _take_source() -> _Source:
    """Step 3 (S21, S28)."""
    from backend.app.api.routes._spoolman_helpers import assert_safe_spoolman_url  # noqa: PLC0415
    from backend.app.services.spoolman_usage_history import normalize_url  # noqa: PLC0415

    if not spoolman_source.guard_free():
        raise _source_changed()
    g0 = spoolman_source.generation()
    rows, data = await _read_source_snapshot()
    if spoolman_source.generation() != g0 or not spoolman_source.guard_free():
        raise _source_changed()
    enabled = (rows.get("spoolman_enabled") or "").strip().lower() == "true"
    url = (rows.get("spoolman_url") or "").strip()
    if not enabled:
        raise HTTPException(status_code=400, detail="Spoolman integration is not enabled")
    if not url:
        raise HTTPException(status_code=400, detail="Spoolman URL is not configured")
    try:
        assert_safe_spoolman_url(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _Source(g0, enabled, url, _pure_token(data, normalize_url(url)))


async def _open_tracking() -> list:
    """Tracking rows of archives still ``printing`` — the prints a final charge can still come from."""
    from backend.app.models.active_print_spoolman import ActivePrintSpoolman  # noqa: PLC0415
    from backend.app.models.archive import PrintArchive  # noqa: PLC0415

    async with database.async_session() as db:
        return list(
            (
                await db.execute(
                    select(ActivePrintSpoolman.printer_id, ActivePrintSpoolman.ams_trays)
                    .join(PrintArchive, PrintArchive.id == ActivePrintSpoolman.archive_id)
                    .where(PrintArchive.status == "printing")
                )
            ).all()
        )


async def _early_refusals(spool_id: int) -> None:
    """Step 2 — economy only: every refusal here is one step 6 would give too."""
    if spoolman_activity.window_open() or spoolman_source.charges_open():
        raise _print_closing()
    feeding, _slots = await _feeding_slots(spool_id, await _open_tracking())
    if feeding:
        raise _feeds_print()
    if await _open_print_event(spool_id):
        raise _in_use(["open_print"])


async def _feeding_slots(spool_id: int, tracking_rows: list) -> tuple[bool, list[tuple[int, int, int]]]:
    """Slots bound to the number, and whether one feeds a print still to be charged."""
    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment  # noqa: PLC0415
    from backend.app.services import spool_single_slot  # noqa: PLC0415

    async with database.async_session() as db:
        slots = [
            tuple(row)
            for row in (
                await db.execute(
                    select(
                        SpoolmanSlotAssignment.printer_id,
                        SpoolmanSlotAssignment.ams_id,
                        SpoolmanSlotAssignment.tray_id,
                    ).where(SpoolmanSlotAssignment.spoolman_spool_id == spool_id)
                )
            ).all()
        ]
    tracked_printers = {printer_id for printer_id, _trays in tracking_rows}
    feeding = any(spool_single_slot.slot_feeds_running_print(*slot) or slot[0] in tracked_printers for slot in slots)
    return feeding, slots


async def _open_print_event(spool_id: int) -> bool:
    from backend.app.models.archive import PrintArchive  # noqa: PLC0415
    from backend.app.models.print_usage_event import PrintUsageEvent  # noqa: PLC0415

    async with database.async_session() as db:
        return (
            await db.execute(
                select(PrintUsageEvent.id)
                .join(PrintArchive, PrintArchive.id == PrintUsageEvent.archive_id)
                .where(PrintUsageEvent.spoolman_spool_id == spool_id, PrintArchive.status == "printing")
                .limit(1)
            )
        ).first() is not None


async def _has_history(spool_id: int, token: str | None, registered: str | None) -> bool:
    from backend.app.models.spoolman_usage_history import SpoolmanUsageHistory  # noqa: PLC0415
    from backend.app.services.spoolman_usage_history import belongs_to  # noqa: PLC0415

    if token is None:
        return False
    async with database.async_session() as db:
        rows = (
            await db.execute(
                select(SpoolmanUsageHistory.spool_registered, SpoolmanUsageHistory.created_at).where(
                    SpoolmanUsageHistory.source_token == token,
                    SpoolmanUsageHistory.spoolman_spool_id == spool_id,
                )
            )
        ).all()
    return any(belongs_to(row.spool_registered, row.created_at, registered) for row in rows)


def _tag_matches(tag: str, native: set[str], extra: str) -> bool:
    from backend.app.services.spoolman import normalize_tag_uid  # noqa: PLC0415

    return normalize_tag_uid(tag) in native or (bool(extra) and tag.strip('"').upper() == extra)


async def _tag_in_open_print(spool: dict, tracking_rows: list) -> bool:
    """Step 6, the tag leg (S16, S26, S34): a running print's final charge could find this spool by a tag."""
    from backend.app.services.spoolman import extra_tag, native_tag_uids  # noqa: PLC0415
    from backend.app.services.spoolman_tracking import _get_printer_serial, guard_tray_tags  # noqa: PLC0415

    native = set(native_tag_uids(spool))
    extra = extra_tag(spool)
    if not native and not extra:
        return False
    for printer_id, ams_trays in tracking_rows:
        trays = {int(k): v for k, v in (ams_trays or {}).items()}
        serial = await _get_printer_serial(printer_id)
        if any(_tag_matches(tag, native, extra) for tag in guard_tray_tags(trays, serial)):
            return True
    return False


def _get_failure(exc: BaseException) -> HTTPException:
    from backend.app.services.spoolman import (  # noqa: PLC0415
        SpoolmanClientError,
        SpoolmanNotFoundError,
        SpoolmanUnavailableError,
    )

    if isinstance(exc, SpoolmanNotFoundError):
        return HTTPException(status_code=404, detail="Spool not found in Spoolman")
    if isinstance(exc, SpoolmanClientError):
        return HTTPException(status_code=502, detail="Spoolman rejected the request")
    if isinstance(exc, SpoolmanUnavailableError):
        return HTTPException(status_code=503, detail="Spoolman server is not reachable")
    raise exc


def _client_for(url: str):
    """This delete's OWN client on S0's address (spec §C1) — never the global one."""
    from backend.app.services.spoolman import SpoolmanClient  # noqa: PLC0415

    return SpoolmanClient(url)


# --- the door ---------------------------------------------------------------------


async def delete_spool(spool_id: int) -> DeleteOutcome:
    """Steps 1–8 for one spool. Raises ``HTTPException`` for every refusal (spec §C7)."""
    if not delete_runtime.admitting:
        raise _paused()
    await _early_refusals(spool_id)
    source = await _take_source()

    f0 = spoolman_binding_fence.snapshot(spool_id)
    if f0 is None:
        raise _state_changed()

    client = _client_for(source.url)
    handed_over = False
    try:
        if not await client.health_check():
            raise HTTPException(status_code=503, detail="Spoolman server is not reachable")
        try:
            spool = await client.get_spool(spool_id)
        except Exception as exc:  # noqa: BLE001 — translated or re-raised by _get_failure
            raise _get_failure(exc) from exc
        registered = spool.get("registered") if isinstance(spool, dict) else None
        registered = str(registered) if registered else None

        if spoolman_activity.window_open() or spoolman_source.charges_open():
            raise _print_closing()
        if spoolman_activity.records_open():
            raise _state_changed()
        a0 = spoolman_activity.revision()

        reasons: list[str] = []
        if await _has_history(spool_id, source.token, registered):
            reasons.append("usage_history")
        tracking_rows = await _open_tracking()
        feeding, slots = await _feeding_slots(spool_id, tracking_rows)
        if await _tag_in_open_print(spool, tracking_rows):
            reasons.append("print_tag")
        if await _open_print_event(spool_id):
            reasons.append("open_print")
        if reasons:
            raise _in_use(reasons)
        if feeding:
            raise _feeds_print()

        task = _try_admit(spool_id, source, a0, f0, slots, client)
        handed_over = True
    finally:
        if not handed_over:
            await _close_quietly(client, spool_id)
    return await asyncio.shield(task)


async def _close_quietly(client, spool_id: int) -> None:
    """Close this delete's own client; a failure is logged, never the delete's answer (acceptance A03).

    The outcome is already decided — a refusal, a confirmed delete, an unknown result — and
    a transport that would not tear down changes none of it."""
    try:
        await client.close()
    except Exception:  # noqa: BLE001 - logged; the decided outcome stands
        logger.warning(
            "[SPOOLMAN] Delete of spool %s: its Spoolman client did not close cleanly", spool_id, exc_info=True
        )


def _try_admit(spool_id: int, source: _Source, a0: int, f0: int, slots, client) -> asyncio.Task:
    """Step 7 — ONE synchronous function after the last await (S10, S19, S21, S24, S31)."""
    from backend.app.services import spool_single_slot  # noqa: PLC0415

    if not delete_runtime.admitting:
        raise _paused()
    if spoolman_source.generation() != source.generation or not spoolman_source.guard_free():
        raise _source_changed()
    if spoolman_activity.revision() != a0 or spoolman_activity.anything_open() or spoolman_source.charges_open():
        raise _state_changed()
    if any(spool_single_slot.slot_feeds_running_print(*slot) for slot in slots):
        raise _state_changed()
    if not spoolman_binding_fence.admit_delete(spool_id, f0):
        raise _state_changed()
    return delete_runtime.register(_delete_and_clean(spool_id, source, client))


async def _delete_and_clean(spool_id: int, source: _Source, client) -> DeleteOutcome:
    """Step 8 (spec §C5): the task the runtime owns from its registration."""
    from backend.app.services.spoolman import (  # noqa: PLC0415
        SpoolmanClientError,
        SpoolmanNotFoundError,
        SpoolmanUnavailableError,
    )

    started = time.monotonic()
    outcome_label = "unknown"
    try:
        try:
            await client.delete_spool(spool_id)
        except SpoolmanNotFoundError as exc:
            outcome_label = "not_found"
            raise HTTPException(status_code=404, detail="Spool not found in Spoolman") from exc
        except SpoolmanClientError as exc:
            outcome_label = "refused"
            raise HTTPException(status_code=502, detail="Spoolman rejected the request") from exc
        except SpoolmanUnavailableError as exc:
            outcome_label = "outcome_unknown"
            timeout = _is_timeout(exc)
            raise _coded(504 if timeout else 502, "spoolman_outcome_unknown") from exc
        outcome = await _clean_up(spool_id, source)
        outcome_label = outcome.warning or "deleted"
        return outcome
    finally:
        spoolman_binding_fence.release_delete(spool_id)
        # Still inside the task the runtime owns: the drain waits for this close attempt too.
        await _close_quietly(client, spool_id)
        logger.info(
            "[SPOOLMAN] Delete of spool %s: %s (%.0f ms)",
            spool_id,
            outcome_label,
            (time.monotonic() - started) * 1000,
        )


def _is_timeout(exc: BaseException) -> bool:
    import httpx  # noqa: PLC0415

    cause = exc.__cause__
    while cause is not None:
        if isinstance(cause, httpx.TimeoutException):
            return True
        cause = cause.__cause__
    return False


async def _clean_up(spool_id: int, source: _Source) -> DeleteOutcome:
    """After a 2xx: one local transaction under the source guard, or a named outcome (spec §C5)."""
    from backend.app.models.spoolman_k_profile import SpoolmanKProfile  # noqa: PLC0415
    from backend.app.services import spool_single_slot  # noqa: PLC0415

    try:
        async with spoolman_source.guard():
            if spoolman_source.generation() != source.generation:
                logger.warning(
                    "[SPOOLMAN] Spool %s deleted, but the Spoolman source changed meanwhile; local links kept",
                    spool_id,
                )
                return DeleteOutcome(warning="local_cleanup_skipped")
            async with database.async_session() as db:
                released = await spool_single_slot.release_every_spoolman_slot(
                    db, spoolman_spool_id=spool_id, refuse=False
                )
                await db.execute(delete(SpoolmanKProfile).where(SpoolmanKProfile.spoolman_spool_id == spool_id))
                await db.commit()
    except Exception:
        logger.exception("[SPOOLMAN] Spool %s deleted in Spoolman, but its local links could not be removed", spool_id)
        return DeleteOutcome(warning="local_cleanup_failed")
    await spool_single_slot.announce(released.vacated)
    if released.feeding:
        return DeleteOutcome(warning="slot_kept_feeding", slots=list(released.feeding))
    return DeleteOutcome()


async def delete_many(spool_ids: list[int]) -> dict:
    """The bulk door (spec §C7): one at a time through :func:`delete_spool`, duplicates dropped."""
    deleted = 0
    warnings: list[dict] = []
    errors: list[dict] = []
    for spool_id in dict.fromkeys(int(s) for s in spool_ids):
        try:
            outcome = await delete_spool(spool_id)
        except HTTPException as exc:
            errors.append(_bulk_error(spool_id, exc))
            continue
        except Exception:
            # One spool's unexpected failure never costs the others their attempt, nor the
            # spools already deleted their place in the answer (acceptance A03).
            logger.exception("[SPOOLMAN] Bulk delete: spool %s failed unexpectedly", spool_id)
            errors.append({"id": spool_id, "status": 500, "detail": "Unexpected error"})
            continue
        deleted += 1
        if outcome.warning:
            warning: dict = {"id": spool_id, "code": outcome.warning}
            if outcome.slots:
                warning["slots"] = [{"printer_id": p, "ams_id": a, "tray_id": t} for p, a, t in outcome.slots]
            warnings.append(warning)
    return {"deleted": deleted, "warnings": warnings, "errors": errors}


def _bulk_error(spool_id: int, exc: HTTPException) -> dict:
    """One refused or failed spool of a bulk delete: its status, code, reasons and text."""
    detail = exc.detail
    error: dict = {"id": spool_id, "status": exc.status_code}
    if isinstance(detail, dict):
        error["detail"] = str(detail.get("message", ""))
        if isinstance(detail.get("error"), str):
            error["code"] = detail["error"]
        if isinstance(detail.get("reasons"), list):
            error["reasons"] = detail["reasons"]
    else:
        error["detail"] = str(detail)
    return error


def reset_for_tests() -> None:
    delete_runtime.admitting = True
    delete_runtime.tasks.clear()
