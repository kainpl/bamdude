"""C047 part 2 — a Spoolman spool printed with is archived, not deleted.

Spec ``60-specs/c047-spool-id-never-reissued-spec.md`` (vault), §C1–§C7 and the
acceptance scenarios r5–r8. Each test names the finding (S16…S44) it pins.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.app.models.active_print_spoolman import ActivePrintSpoolman
from backend.app.models.archive import PrintArchive
from backend.app.models.print_usage_event import PrintUsageEvent
from backend.app.models.settings import Settings
from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
from backend.app.models.spoolman_usage_history import SpoolmanUsageHistory
from backend.app.services import (
    spoolman_activity,
    spoolman_binding_fence as fence,
    spoolman_charge,
    spoolman_delete,
    spoolman_source,
    spoolman_source_changes,
    spoolman_tracking,
    spoolman_usage_history as history,
)
from backend.app.services.spoolman_binding_fence import SpoolBeingDeleted

URL = "http://spoolman.test:7912"
SERIAL = "01P00A000000001"
SPOOL = 42


# --- fixtures -----------------------------------------------------------------


async def _spoolman(db, *, enabled: bool = True, url: str = URL) -> None:
    db.add_all(
        [
            Settings(key="spoolman_enabled", value="true" if enabled else "false"),
            Settings(key="spoolman_url", value=url),
        ]
    )
    await db.commit()


async def _token(db) -> str | None:
    return await history.source_token(db)


class _Client:
    """Spoolman as the delete door sees it: health, one GET, one DELETE."""

    def __init__(self, spool: dict | None = None, *, delete_error: BaseException | None = None):
        self.spool = spool or {"id": SPOOL, "registered": "2026-10-01T08:00:00Z", "extra": {}, "tags": []}
        self.delete_error = delete_error
        self.deleted: list[int] = []
        self.closed = False
        self.gets = 0

    async def health_check(self) -> bool:
        return True

    async def get_spool(self, spool_id: int) -> dict:
        self.gets += 1
        return dict(self.spool)

    async def delete_spool(self, spool_id: int) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(spool_id)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def client(monkeypatch):
    fake = _Client()
    built: list[str] = []

    def _factory(url):
        built.append(url)
        return fake

    monkeypatch.setattr(spoolman_delete, "_client_for", _factory)
    monkeypatch.setattr(spoolman_tracking, "_get_printer_serial", AsyncMock(return_value=SERIAL))
    fake.built = built
    return fake


async def _printing(db, printer, *, tracking_trays: dict | None = None, journal_spool: int | None = None):
    archive = PrintArchive(
        printer_id=printer.id, filename="a.3mf", file_path="", file_size=0, print_name="a", status="printing"
    )
    db.add(archive)
    await db.commit()
    if tracking_trays is not None:
        db.add(
            ActivePrintSpoolman(
                printer_id=printer.id,
                archive_id=archive.id,
                filament_usage=[{"slot_id": 1, "used_g": 5.0}],
                ams_trays=tracking_trays,
                slot_to_tray=[254],
            )
        )
    if journal_spool is not None:
        db.add(
            PrintUsageEvent(
                printer_id=printer.id,
                archive_id=archive.id,
                layer_num=0,
                event="start",
                spoolman_spool_id=journal_spool,
            )
        )
    await db.commit()
    return archive


# --- the fence (§C3, S31) ----------------------------------------------------------


class TestTheFence:
    def test_a_ticket_taken_during_the_lease_is_refused_after_its_release(self):
        """S31: the release writes a new fence; a ticket from inside the lease is below it."""
        f0 = fence.snapshot(SPOOL)
        assert fence.admit_delete(SPOOL, f0)
        issued = fence.ticket()  # a writer's GET still saw the spool
        fence.release_delete(SPOOL)
        with pytest.raises(SpoolBeingDeleted), fence.enter([SPOOL], issued):
            pass
        with fence.enter([SPOOL], fence.ticket()):  # a fresh proof passes
            pass

    def test_the_global_revision_moving_on_another_number_does_not_save_a_stale_ticket(self):
        assert fence.admit_delete(SPOOL, fence.snapshot(SPOOL))
        with fence.enter([7], fence.ticket()):
            pass
        issued = fence.ticket()
        fence.release_delete(SPOOL)
        with pytest.raises(SpoolBeingDeleted), fence.tag_operation([SPOOL], issued=issued):
            pass

    def test_a_held_mark_keeps_a_delete_out(self):
        with fence.tag_operation([SPOOL]):
            assert fence.snapshot(SPOOL) is None
        f0 = fence.snapshot(SPOOL)
        assert f0 is not None and fence.admit_delete(SPOOL, f0)

    def test_a_mark_taken_and_released_since_f0_refuses_the_admission(self):
        """S21: a tag change between the GET's answer and its delivery moves the number's revision."""
        f0 = fence.snapshot(SPOOL)
        with fence.tag_operation([SPOOL]):
            pass
        assert not fence.admit_delete(SPOOL, f0)

    def test_an_operation_is_all_or_nothing(self):
        assert fence.admit_delete(SPOOL, fence.snapshot(SPOOL))
        with pytest.raises(SpoolBeingDeleted), fence.tag_operation([7, SPOOL]):
            pass
        assert fence.snapshot(7) is not None, "the number taken first was given back"

    def test_a_created_number_says_so(self):
        post_ticket = fence.ticket()
        assert fence.admit_delete(SPOOL, fence.snapshot(SPOOL))
        with fence.tag_operation() as operation, pytest.raises(SpoolBeingDeleted) as refused:
            operation.add(SPOOL, created=True, issued=post_ticket)
        assert refused.value.created

    def test_a_delete_finished_after_the_post_ticket_refuses_the_created_number(self):
        """A02: the create's own ticket sees a delete admitted AND released while the POST ran."""
        with fence.tag_operation(issued=fence.ticket()) as operation:
            post_ticket = fence.ticket()
            assert fence.admit_delete(SPOOL, fence.snapshot(SPOOL))
            fence.release_delete(SPOOL)  # the delete is over before the POST answers
            with pytest.raises(SpoolBeingDeleted) as refused:
                operation.add(SPOOL, created=True, issued=post_ticket)
        assert refused.value.created and refused.value.number == SPOOL

    def test_a_number_deleted_before_the_post_is_the_new_spools(self):
        """A02: Spoolman may give a deleted number to the new spool; a delete over before the POST is no refusal."""
        assert fence.admit_delete(SPOOL, fence.snapshot(SPOOL))
        fence.release_delete(SPOOL)
        with fence.tag_operation() as operation:
            operation.add(SPOOL, created=True, issued=fence.ticket())
            assert operation.owns(SPOOL)

    def test_a_created_number_needs_its_post_ticket(self):
        with fence.tag_operation() as operation, pytest.raises(ValueError):
            operation.add(SPOOL, created=True)

    def test_an_owned_number_is_not_judged_by_the_older_batch_ticket(self):
        """A02: a batch commit's guard does not refuse a number the operation proved later and still marks."""
        batch_ticket = fence.ticket()
        assert fence.admit_delete(SPOOL, fence.snapshot(SPOOL))
        fence.release_delete(SPOOL)
        with fence.tag_operation(issued=batch_ticket) as operation:
            operation.add(SPOOL, created=True, issued=fence.ticket())
            with fence.enter([SPOOL], batch_ticket):
                pass
        with pytest.raises(SpoolBeingDeleted), fence.enter([SPOOL], batch_ticket):
            pass  # outside the operation the old ticket still refuses


# --- the source (§C1) ---------------------------------------------------------------


class TestTheSource:
    async def test_the_guard_is_not_reentrant(self):
        async with spoolman_source.guard():
            with pytest.raises(RuntimeError):
                async with spoolman_source.guard():
                    pass

    async def test_a_reserved_writer_is_not_a_clean_session(self, db_session):
        """S30: a no-op UPDATE reserves the writer and leaves ``db.new`` empty."""
        from backend.app.core.credential_config import lock_credential_config

        await lock_credential_config(db_session)
        assert not db_session.new
        with pytest.raises(RuntimeError):
            async with spoolman_source.guard(db_session):
                pass
        await db_session.rollback()

    async def test_the_first_token_raises_the_generation_and_a_known_one_does_not(self, db_session):
        """S42."""
        await _spoolman(db_session)
        g = spoolman_source.generation()
        assert await history.current_token(db_session) is None, "a reader allocates no UUID"
        first = await _token(db_session)
        assert first and spoolman_source.generation() == g + 1
        assert await _token(db_session) == first and spoolman_source.generation() == g + 1

    async def test_readers_write_nothing(self, db_session):
        """S39: the state of the card comes from the pure resolution."""
        await _spoolman(db_session)
        await _token(db_session)
        (
            await db_session.execute(select(Settings).where(Settings.key == "spoolman_url"))
        ).scalar_one().value = "http://other.test:7912"
        await db_session.commit()
        before = (
            await db_session.execute(select(Settings.value).where(Settings.key == history.SOURCE_SETTING))
        ).scalar_one()
        state = await history.source_state(db_session)
        assert state["has_source"] is True and await history.current_token(db_session) is None
        after = (
            await db_session.execute(select(Settings.value).where(Settings.key == history.SOURCE_SETTING))
        ).scalar_one()
        assert before == after


# --- base changes and the first facts of a print (S38, S41, S44) ----------------------


class TestBaseChanges:
    async def _change_url(self, db, url):
        async with spoolman_source_changes.owning(db) as change:
            await change.decide(db, url=url)
            (await db.execute(select(Settings).where(Settings.key == "spoolman_url"))).scalar_one().value = url
            await db.commit()

    async def test_a_new_address_is_refused_while_a_print_accounts_in_spoolman(self, db_session, printer_factory):
        await _spoolman(db_session)
        await _printing(db_session, await printer_factory())
        with pytest.raises(HTTPException) as refused:
            await self._change_url(db_session, "http://b.test:7912")
        assert refused.value.status_code == 409
        assert refused.value.detail["error"] == "spoolman_print_in_progress"
        assert refused.value.detail["reason"] == "print_accounting"
        assert not spoolman_source.base_change_in_flight()

    async def test_a_journal_number_protects_the_base_with_spoolman_off(self, db_session, printer_factory):
        await _spoolman(db_session, enabled=False)
        await _printing(db_session, await printer_factory(), journal_spool=17)
        with pytest.raises(HTTPException):
            await self._change_url(db_session, "http://b.test:7912")

    async def test_mode_and_same_database_are_not_base_changes(self, db_session, printer_factory):
        await _spoolman(db_session)
        await _printing(db_session, await printer_factory())
        async with spoolman_source_changes.owning(db_session) as change:
            assert await change.decide(db_session, enabled="false")
            await db_session.rollback()

    async def test_any_change_is_refused_while_a_charge_is_open(self, db_session):
        await _spoolman(db_session)
        ownership = spoolman_source.register_charge("test")
        try:
            with pytest.raises(HTTPException) as refused:
                async with spoolman_source_changes.owning(db_session) as change:
                    await change.decide(db_session, enabled="false")
            assert refused.value.detail["reason"] == "charge_window"
        finally:
            spoolman_source.release_charge(ownership)

    async def test_an_open_record_refuses_the_flag(self, db_session):
        """S44 step 1: the activity check and the flag are one step."""
        await _spoolman(db_session)
        with spoolman_activity.record("print_start"), pytest.raises(HTTPException) as refused:
            await self._change_url(db_session, "http://b.test:7912")
        assert refused.value.detail["reason"] == "print_accounting"

    async def test_a_first_fact_waits_for_the_flag_and_publishes_after(self):
        """S44: a publisher that comes during the base change reads after its commit."""
        order: list[str] = []
        async with spoolman_source.guard():
            spoolman_source.begin_base_change()

            async def publisher():
                async with spoolman_activity.first_evidence("print_start"):
                    order.append("published")

            task = asyncio.create_task(publisher())
            await asyncio.sleep(0.05)
            order.append("committed")
            spoolman_source.end_base_change()
        await asyncio.wait_for(task, 1)
        assert order == ["committed", "published"]


# --- the charge (S33, S35, S40) ---------------------------------------------------


class TestTheCharge:
    async def test_a_closed_gate_writes_nothing(self, db_session):
        await _spoolman(db_session)
        spoolman_source.close_charge_admission()
        assert await spoolman_charge.admit("test") is None
        assert (
            await db_session.execute(select(Settings).where(Settings.key == history.SOURCE_SETTING))
        ).scalar_one_or_none() is None

    async def test_the_gate_closing_while_waiting_registers_nothing_and_persists_nothing(self, db_session):
        """S40: the gate is checked again together with the registration, before the first write."""
        await _spoolman(db_session)
        async with spoolman_source.guard():
            waiting = asyncio.create_task(spoolman_charge.admit("test"))
            await asyncio.sleep(0.05)
            spoolman_source.close_charge_admission()
        assert await asyncio.wait_for(waiting, 1) is None
        assert spoolman_source.charges_open() == 0
        assert (
            await db_session.execute(select(Settings).where(Settings.key == history.SOURCE_SETTING))
        ).scalar_one_or_none() is None

    async def test_the_owner_carries_the_token_of_its_snapshot(self, db_session):
        await _spoolman(db_session)
        async with spoolman_charge.owning("test") as charge:
            assert charge.enabled and charge.url == URL
            assert charge.token == await history.current_token(db_session)
            assert spoolman_source.charges_open() == 1
        assert spoolman_source.charges_open() == 0

    async def test_the_drain_waits_for_a_registered_owner(self, db_session):
        await _spoolman(db_session)
        async with spoolman_charge.owning("test"):
            assert not await spoolman_delete.delete_runtime.quiesce_for_restore(timeout=0.05)
        assert not spoolman_source.charge_gate_open()


# --- the one tag rule (S26, S34) ----------------------------------------------------


class TestTheTagRule:
    def test_the_external_holders_get_a_fallback_without_a_record(self):
        tags = spoolman_tracking.guard_tray_tags({0: {"tray_uuid": "A" * 32}}, SERIAL)
        assert spoolman_tracking.attribution_tray_tag({}, 254, SERIAL) in tags
        assert spoolman_tracking.attribution_tray_tag({}, 255, SERIAL) in tags

    def test_an_invalid_or_empty_tray_gets_none(self):
        assert spoolman_tracking.attribution_tray_tag({}, 256, SERIAL) == ""
        assert spoolman_tracking.attribution_tray_tag({3: {}}, 3, SERIAL) == ""

    def test_a_recorded_tray_gives_its_rfid(self):
        assert spoolman_tracking.attribution_tray_tag({3: {"tray_uuid": "B" * 32}}, 3, SERIAL) == "B" * 32


# --- the door (§C2, §C5, §C7) -------------------------------------------------------


class TestTheDoor:
    async def test_a_spool_without_history_is_deleted_and_leaves_its_slots(self, db_session, printer_factory, client):
        await _spoolman(db_session)
        printer = await printer_factory()
        db_session.add(SpoolmanSlotAssignment(printer_id=printer.id, ams_id=0, tray_id=1, spoolman_spool_id=SPOOL))
        await db_session.commit()
        outcome = await spoolman_delete.delete_spool(SPOOL)
        assert outcome.as_response() == {"status": "deleted"}
        assert client.deleted == [SPOOL] and client.closed and client.built == [URL]
        rows = (await db_session.execute(select(SpoolmanSlotAssignment))).scalars().all()
        assert rows == []
        assert not fence.deleting(SPOOL)

    async def test_history_of_the_current_source_refuses_with_zero_delete(self, db_session, client):
        await _spoolman(db_session)
        token = await _token(db_session)
        db_session.add(
            SpoolmanUsageHistory(
                created_at=datetime(2026, 11, 1),
                source_token=token,
                spoolman_spool_id=SPOOL,
                weight_used=5.0,
                spool_registered="2026-10-01T08:00:00Z",
            )
        )
        await db_session.commit()
        with pytest.raises(HTTPException) as refused:
            await spoolman_delete.delete_spool(SPOOL)
        assert refused.value.detail["error"] == "spool_in_use"
        assert refused.value.detail["reasons"] == ["usage_history"]
        assert client.deleted == []

    async def test_an_earlier_holders_history_does_not_refuse(self, db_session, client):
        await _spoolman(db_session)
        token = await _token(db_session)
        db_session.add(
            SpoolmanUsageHistory(
                created_at=datetime(2026, 11, 1),
                source_token=token,
                spoolman_spool_id=SPOOL,
                weight_used=5.0,
                spool_registered="2025-01-01T08:00:00Z",
            )
        )
        await db_session.commit()
        await spoolman_delete.delete_spool(SPOOL)
        assert client.deleted == [SPOOL]

    async def test_a_disabled_mode_in_s0_is_400_with_no_http(self, db_session, client):
        """S28."""
        await _spoolman(db_session, enabled=False)
        with pytest.raises(HTTPException) as refused:
            await spoolman_delete.delete_spool(SPOOL)
        assert refused.value.status_code == 400
        assert client.built == [] and client.gets == 0

    async def test_the_external_holders_fallback_tag_refuses(self, db_session, printer_factory, client):
        """S26: ``slot_to_tray=[254]``, no 254 in the snapshot, the spool known only by the fallback tag."""
        await _spoolman(db_session)
        printer = await printer_factory()
        await _printing(db_session, printer, tracking_trays={0: {"tray_uuid": "C" * 32}})
        client.spool["extra"] = {"tag": json.dumps(spoolman_tracking.attribution_tray_tag({}, 254, SERIAL))}
        with pytest.raises(HTTPException) as refused:
            await spoolman_delete.delete_spool(SPOOL)
        assert refused.value.detail["reasons"] == ["print_tag"]
        assert client.deleted == []

    async def test_an_open_journal_number_refuses(self, db_session, printer_factory, client):
        await _spoolman(db_session)
        await _printing(db_session, await printer_factory(), journal_spool=SPOOL)
        with pytest.raises(HTTPException) as refused:
            await spoolman_delete.delete_spool(SPOOL)
        assert refused.value.detail["error"] == "spool_in_use"
        assert client.deleted == []

    async def test_an_open_window_is_print_closing(self, db_session, client):
        await _spoolman(db_session)
        spoolman_activity.enter_window()
        try:
            with pytest.raises(HTTPException) as refused:
                await spoolman_delete.delete_spool(SPOOL)
        finally:
            spoolman_activity.leave_window()
        assert refused.value.detail["error"] == "print_closing"

    async def test_a_first_token_during_the_get_refuses_the_admission(self, db_session, client, monkeypatch):
        """S42: S0 saw no token; a charge allocated it and committed history while the GET ran."""
        await _spoolman(db_session)
        real_get = client.get_spool

        async def get_while_a_charge_lands(spool_id):
            async with history_db() as db:
                await history.source_token(db)
            return await real_get(spool_id)

        from backend.app.core import database

        history_db = database.async_session
        monkeypatch.setattr(client, "get_spool", get_while_a_charge_lands)
        with pytest.raises(HTTPException) as refused:
            await spoolman_delete.delete_spool(SPOOL)
        assert refused.value.detail["error"] == "spoolman_source_changed"
        assert client.deleted == []

    async def test_an_unknown_outcome_runs_no_cleanup(self, db_session, printer_factory, client):
        from backend.app.services.spoolman import SpoolmanUnavailableError

        await _spoolman(db_session)
        printer = await printer_factory()
        db_session.add(SpoolmanSlotAssignment(printer_id=printer.id, ams_id=0, tray_id=1, spoolman_spool_id=SPOOL))
        await db_session.commit()
        client.delete_error = SpoolmanUnavailableError("down")
        with pytest.raises(HTTPException) as refused:
            await spoolman_delete.delete_spool(SPOOL)
        assert refused.value.detail["error"] == "spoolman_outcome_unknown"
        assert (await db_session.execute(select(SpoolmanSlotAssignment))).scalars().all() != []
        assert not fence.deleting(SPOOL), "the lease goes after an unknown outcome too"

    async def test_a_closed_runtime_is_paused(self, db_session, client):
        await _spoolman(db_session)
        spoolman_delete.delete_runtime.close_admission()
        with pytest.raises(HTTPException) as refused:
            await spoolman_delete.delete_spool(SPOOL)
        assert refused.value.status_code == 503
        assert client.gets == 0

    async def test_bulk_drops_duplicates_and_keeps_confirmed_deletes_out_of_errors(self, db_session, client):
        await _spoolman(db_session)
        token = await _token(db_session)
        db_session.add(
            SpoolmanUsageHistory(
                created_at=datetime(2026, 11, 1), source_token=token, spoolman_spool_id=8, weight_used=1.0
            )
        )
        await db_session.commit()
        client.spool = {"id": 0, "extra": {}, "tags": []}
        result = await spoolman_delete.delete_many([SPOOL, 8, SPOOL])
        assert result["deleted"] == 1
        assert [e["id"] for e in result["errors"]] == [8]
        assert result["errors"][0]["code"] == "spool_in_use"
        assert client.deleted == [SPOOL]

    async def test_a_registered_task_finishes_after_its_caller_is_cancelled(self, db_session, client):
        await _spoolman(db_session)
        released = asyncio.Event()

        async def slow_delete(spool_id):
            await released.wait()
            client.deleted.append(spool_id)

        client.delete_spool = slow_delete
        caller = asyncio.create_task(spoolman_delete.delete_spool(SPOOL))
        while not spoolman_delete.delete_runtime.tasks:
            await asyncio.sleep(0.01)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert not await spoolman_delete.delete_runtime.quiesce_for_restore(timeout=0.05), "the drain sees it"
        released.set()
        (task,) = list(spoolman_delete.delete_runtime.tasks)
        await asyncio.wait_for(task, 1)
        assert client.deleted == [SPOOL]

    async def test_a_failed_close_keeps_the_confirmed_delete(self, db_session, client, monkeypatch):
        """A03: the transport's teardown is no part of the answer."""
        await _spoolman(db_session)
        monkeypatch.setattr(client, "close", AsyncMock(side_effect=OSError("teardown failed")))
        outcome = await spoolman_delete.delete_spool(SPOOL)
        assert outcome.as_response() == {"status": "deleted"}
        assert client.deleted == [SPOOL] and not fence.deleting(SPOOL)
        assert not spoolman_delete.delete_runtime.tasks, "the task ended after its close attempt"

    async def test_a_failed_close_keeps_a_refusal(self, db_session, client, monkeypatch):
        await _spoolman(db_session)
        spoolman_activity.enter_window()
        monkeypatch.setattr(client, "close", AsyncMock(side_effect=OSError("teardown failed")))
        try:
            with pytest.raises(HTTPException) as refused:
                await spoolman_delete.delete_spool(SPOOL)
        finally:
            spoolman_activity.leave_window()
        assert refused.value.detail["error"] == "print_closing"

    async def test_a_failed_close_does_not_stop_the_bulk(self, db_session, client, monkeypatch):
        """A03: a confirmed delete keeps its place in the answer and the next spool is tried."""
        await _spoolman(db_session)
        monkeypatch.setattr(client, "close", AsyncMock(side_effect=OSError("teardown failed")))
        client.spool = {"id": 0, "extra": {}, "tags": []}
        result = await spoolman_delete.delete_many([17, 18])
        assert result == {"deleted": 2, "warnings": [], "errors": []}
        assert client.deleted == [17, 18]

    async def test_an_unexpected_failure_is_one_bulk_error(self, db_session, client, monkeypatch):
        await _spoolman(db_session)
        real = spoolman_delete.delete_spool

        async def one_breaks(spool_id):
            if spool_id == 17:
                raise RuntimeError("boom")
            return await real(spool_id)

        monkeypatch.setattr(spoolman_delete, "delete_spool", one_breaks)
        client.spool = {"id": 0, "extra": {}, "tags": []}
        result = await spoolman_delete.delete_many([17, 18])
        assert result["deleted"] == 1 and [e["id"] for e in result["errors"]] == [17]
        assert result["errors"][0]["status"] == 500
        assert client.deleted == [18]


# --- a restart replay owns its record; its numbers carry their base (acceptance A01) ---


@pytest.fixture
def journal_state():
    from backend.app.services import print_usage_journal as journal

    journal.forget_tail_state()
    yield journal
    journal.forget_tail_state()


def _queue(journal, printer_id, ids, *, stamp):
    from backend.app.services.bambu_mqtt import ReplayBatch, ReplayFact

    batch = ReplayBatch(1, None, (ReplayFact("tail_start", 0, 10, False, ids),), bindings_stamp=stamp)
    journal.queue_replay(printer_id, [batch])


async def _events(db):
    return (await db.execute(select(PrintUsageEvent.spool_id, PrintUsageEvent.spoolman_spool_id))).all()


class TestTheReplayOwnsItsRecord:
    async def test_a_cancelled_holder_leaves_the_writer_its_record(
        self, db_session, printer_factory, journal_state, monkeypatch
    ):
        """A01: the writer's record outlives its cancelled caller and ends after the writer's commit."""
        printer = await printer_factory()
        await _printing(db_session, printer)
        _queue(journal_state, printer.id, (None, 17), stamp=spoolman_source.base_generation())
        paused, resume = asyncio.Event(), asyncio.Event()
        original = journal_state._write_replay_batch

        async def pause_before_commit(db, printer_id, batch):
            written = await original(db, printer_id, batch)
            paused.set()
            await resume.wait()
            return written

        monkeypatch.setattr(journal_state, "_write_replay_batch", pause_before_commit)

        async def holder():
            async with journal_state.journal_lock(printer.id):
                pass

        caller = asyncio.create_task(holder())
        await asyncio.wait_for(paused.wait(), 2)
        writer = journal_state._replay_writers[printer.id]
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert not writer.done()
        assert spoolman_activity.records_open(), "the writer keeps its own record"
        async with spoolman_source.guard():
            with pytest.raises(spoolman_source.SourceRefused) as refused:
                spoolman_source.begin_base_change()
        assert refused.value.reason == spoolman_source.PRINT_ACCOUNTING
        resume.set()
        await asyncio.wait_for(writer, 2)
        assert not spoolman_activity.records_open()
        assert await _events(db_session) == [(None, 17)], "written once"

    async def test_a_writer_cancelled_before_it_ran_releases_its_record(self, journal_state):
        _queue(journal_state, 1, (None, 17), stamp=0)
        writer = journal_state._start_replay_writer(asyncio.get_running_loop(), 1)
        assert spoolman_activity.records_open()
        writer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writer
        assert not spoolman_activity.records_open()
        assert journal_state._pending_replays.get(1), "the batch waits for the next holder"

    async def test_a_failed_write_releases_and_the_next_holder_writes_it(
        self, db_session, printer_factory, journal_state, monkeypatch
    ):
        printer = await printer_factory()
        await _printing(db_session, printer)
        _queue(journal_state, printer.id, (None, 17), stamp=spoolman_source.base_generation())
        original = journal_state._write_replay_batch
        calls = []

        async def fail_once(db, printer_id, batch):
            calls.append(1)
            if len(calls) == 1:
                await original(db, printer_id, batch)
                raise RuntimeError("the first write rolls back")
            return await original(db, printer_id, batch)

        monkeypatch.setattr(journal_state, "_write_replay_batch", fail_once)
        async with journal_state.journal_lock(printer.id):
            pass
        assert not spoolman_activity.records_open() and await _events(db_session) == []
        async with journal_state.journal_lock(printer.id):
            pass
        assert not spoolman_activity.records_open()
        assert await _events(db_session) == [(None, 17)]


class TestTheBindingsStamp:
    async def _bump_base(self):
        async with spoolman_source.guard():
            with spoolman_source.base_change():
                spoolman_source.bump_base_generation()

    async def test_a_number_read_under_another_base_is_written_unknown(
        self, db_session, printer_factory, journal_state
    ):
        """A01: a Spoolman number that outlived its hold across a base change is not written; the built-in id is."""
        printer = await printer_factory()
        await _printing(db_session, printer)
        _queue(journal_state, printer.id, (5, 17), stamp=spoolman_source.base_generation())
        await self._bump_base()
        async with journal_state.journal_lock(printer.id):
            pass
        assert await _events(db_session) == [(5, None)]

    async def test_the_same_base_keeps_the_number(self, db_session, printer_factory, journal_state):
        printer = await printer_factory()
        await _printing(db_session, printer)
        await self._bump_base()
        _queue(journal_state, printer.id, (5, 17), stamp=spoolman_source.base_generation())
        async with journal_state.journal_lock(printer.id):
            pass
        assert await _events(db_session) == [(5, 17)]

    async def test_an_unstamped_batch_writes_no_spoolman_number(self, db_session, printer_factory, journal_state):
        printer = await printer_factory()
        await _printing(db_session, printer)
        _queue(journal_state, printer.id, (5, 17), stamp=None)
        async with journal_state.journal_lock(printer.id):
            pass
        assert await _events(db_session) == [(5, None)]

    async def test_the_base_moves_only_inside_a_base_change(self):
        with pytest.raises(RuntimeError):
            spoolman_source.bump_base_generation()  # no guard
        async with spoolman_source.guard():
            with pytest.raises(RuntimeError):
                spoolman_source.bump_base_generation()  # the guard, but no base change
        assert spoolman_source.base_generation() == 0

    def test_the_batch_carries_the_smallest_stamp_of_its_sources(self):
        from backend.app.services.bambu_mqtt import BambuMQTTClient
        from backend.tests.unit.services.test_tail_refill_latch import ORIGIN, ams, feed, first_packet, refill

        def joined():
            c = BambuMQTTClient(ip_address="1.2.3.4", serial_number="20PC047A01", access_code="1")
            c._previous_gcode_state = "RUNNING"
            c._previous_gcode_file = "plate.3mf"
            c.state.raw_data = {"ams": ams(""), "vt_tray": [{"id": 254}, {"id": 255}]}
            c.on_tray_change = MagicMock()
            c.on_usage_event = MagicMock()
            c.on_feed_event = MagicMock()
            c.on_print_running_observed = MagicMock()
            first_packet(c, 254)
            refill(c)
            feed(c, 254, layer=41)
            feed(c, ORIGIN, layer=43)
            return c

        def seeded(held_stamp, *, held=True):
            c = joined()
            generation = c.journal_generation()
            if held:
                assert c.note_held_binding_change(generation, ORIGIN, (5, 17), (6, 18), stamp=held_stamp)
            assert c.seed_usage_journal(
                generation,
                log=[(ORIGIN, 0)],
                last_loaded_tray=ORIGIN,
                tail_origin=ORIGIN,
                bindings={ORIGIN: (5, 17)},
                bindings_stamp=5,
            )
            (batch,) = c.take_replay_batches()
            return batch.bindings_stamp

        assert seeded(3) == 3, "a held change read earlier"
        assert seeded(None) is None, "an unstamped held change"
        assert seeded(None, held=False) == 5, "the snapshot alone"


# --- the admission comes before the lane (acceptance A05) -----------------------------


class TestTheAdmissionBeforeTheLane:
    async def test_a_nested_admission_in_the_same_task_reuses_the_record(self):
        async with spoolman_activity.first_evidence("outer"):
            revision = spoolman_activity.revision()
            async with spoolman_activity.first_evidence("inner"):
                assert spoolman_activity.revision() == revision
                assert "inner" not in spoolman_activity._activity.open
        assert not spoolman_activity.records_open()

    async def test_a_child_task_is_admitted_on_its_own(self):
        async with spoolman_activity.first_evidence("outer"):

            async def child():
                async with spoolman_activity.first_evidence("child"):
                    return "child" in spoolman_activity._activity.open

            assert await asyncio.create_task(child())

    async def test_the_lane_stays_free_while_a_stock_insertion_waits(self, db_session, printer_factory, monkeypatch):
        from backend.app import main

        printer = await printer_factory()
        claim = AsyncMock(return_value={"reason": "not_stock"})
        monkeypatch.setattr("backend.app.services.auto_stock_spool.claim_on_insertion", claim)
        event = {"ams_id": 0, "tray_id": 0, "generation": 1, "sequence": 1}
        main._stock_insertion_seen.pop((printer.id, 0, 0), None)
        lane = main._get_ams_assignment_lock(printer.id)
        async with spoolman_source.guard():
            spoolman_source.begin_base_change()
            try:
                waiting = asyncio.create_task(main.on_stock_spool_inserted(printer.id, event))
                await asyncio.sleep(0.05)
                assert not waiting.done()
                assert not lane.locked(), "the lane is not held while the admission waits"
                async with asyncio.timeout(1), lane:
                    pass
            finally:
                spoolman_source.end_base_change()
        await asyncio.wait_for(waiting, 2)
        claim.assert_awaited_once()


# --- writers meet the fence (S27, S32, S37) -----------------------------------------


class TestWriters:
    async def test_a_tag_primitive_refuses_a_spool_being_deleted(self):
        from backend.app.services.spoolman import SpoolmanClient

        assert fence.admit_delete(SPOOL, fence.snapshot(SPOOL))
        client = SpoolmanClient(URL)
        client._tag_request = AsyncMock(return_value=SimpleNamespace(status_code=200))
        with pytest.raises(SpoolBeingDeleted):
            await client.link_native_tag(SPOOL, "AABB")
        client._tag_request.assert_not_awaited()

    async def test_the_sync_ledger_refuses_a_stale_ticket(self, db_session, printer_factory):
        from backend.app.services.spoolman_slot_ledger import write_sync_results

        printer = await printer_factory()
        issued = fence.ticket()
        assert fence.admit_delete(SPOOL, fence.snapshot(SPOOL))
        fence.release_delete(SPOOL)
        with pytest.raises(SpoolBeingDeleted):
            await write_sync_results(
                db_session, printer_id=printer.id, read={}, changes=[(0, 1, SPOOL)], empties=[], issued=issued
            )
        assert (await db_session.execute(select(SpoolmanSlotAssignment))).scalars().all() == []

    async def test_the_migrate_report_names_a_spool_being_deleted(self):
        from backend.app.services import spoolman_native_tags

        assert fence.admit_delete(SPOOL, fence.snapshot(SPOOL))
        client = MagicMock()
        client.has_tag_api = AsyncMock(return_value=True)
        client.get_all_spools = AsyncMock(
            return_value=[{"id": SPOOL, "extra": {"tag": json.dumps("AABBCCDD")}, "tags": []}]
        )
        client.get_filaments = AsyncMock(return_value=[])

        async def link(spool_id, uid, tag_format=None):
            with fence.mutating(spool_id):
                return None

        client.link_native_tag = link
        report = await spoolman_native_tags.copy_extra_tags_to_native(client, [], dry_run=False)
        assert {"spool_id": SPOOL, "tag": "AABBCCDD", "reason": "being_deleted"} in report["failed"]
