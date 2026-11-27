"""A Spoolman binding names its spool by number AND ``registered`` (spec spoolman-binding-identity).

Spoolman hands the number of a deleted spool to the next one, and another database behind
the address reuses every number. A slot binding or K link that kept the number alone sent
the next charge, weight, drying stamp or K to whatever spool held the number then.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.filament_calibration import FilamentCalibration
from backend.app.models.settings import Settings
from backend.app.models.spoolman_k_profile import SpoolmanKProfile
from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
from backend.app.services import spoolman_source_changes, spoolman_spool_identity as identity
from backend.app.services.spoolman import (
    SpoolmanClient,
    SpoolmanNotFoundError,
    SpoolmanUnavailableError,
    says_spool_not_found,
)
from backend.app.services.spoolman_kprofile_link import resolve_spoolman_slot_kprofile
from backend.app.services.spoolman_slot_ledger import SyncChange, write_sync_results

OLD = "2026-10-01T08:00:00Z"  # the spool the binding was made for
NEW = "2026-11-20T09:30:00Z"  # another spool Spoolman later gave the same number
NUMBER = 12


async def _spoolman_on(db, url: str = "http://spoolman.test:7912") -> None:
    db.add_all([Settings(key="spoolman_enabled", value="true"), Settings(key="spoolman_url", value=url)])
    await db.commit()


async def _calibration(db, printer_id: int, filament_id: str = "GFG99") -> FilamentCalibration:
    fc = FilamentCalibration(
        printer_id=printer_id,
        filament_id=filament_id,
        nozzle_diameter=0.4,
        nozzle_volume_type="standard",
        extruder_id=0,
        pa_k_value=0.02,
        cali_mode="pa_line",
        source="printer_sync",
        is_active=True,
        name="k",
    )
    db.add(fc)
    await db.commit()
    return fc


async def _bind(db, printer_id: int, *, tray: int = 0, number: int = NUMBER, stamp: str | None = OLD) -> None:
    db.add(
        SpoolmanSlotAssignment(
            printer_id=printer_id, ams_id=0, tray_id=tray, spoolman_spool_id=number, spool_registered=stamp
        )
    )
    await db.commit()


async def _link(db, printer_id: int, fc_id: int, *, number: int = NUMBER, stamp: str | None = OLD) -> None:
    db.add(
        SpoolmanKProfile(
            spoolman_spool_id=number,
            spool_registered=stamp,
            printer_id=printer_id,
            extruder=0,
            filament_calibration_id=fc_id,
        )
    )
    await db.commit()


async def _rows(db, model):
    db.expire_all()
    return (await db.execute(select(model).where(model.spoolman_spool_id == NUMBER))).scalars().all()


async def _printing(db, printer_id: int) -> None:
    db.add(
        PrintArchive(
            printer_id=printer_id, filename="a.3mf", file_path="", file_size=0, print_name="a", status="printing"
        )
    )
    await db.commit()


# --- the rule ------------------------------------------------------------------


class TestVerdict:
    def test_the_same_moment_in_two_spellings_is_the_same_spool(self):
        assert identity.verdict("2026-10-01T08:00:00Z", {"registered": "2026-10-01T08:00:00+00:00"}) == identity.SAME

    def test_another_stamp_is_another_spool(self):
        assert identity.verdict(OLD, {"registered": NEW}) == identity.OTHER

    def test_a_row_without_a_stamp_is_unknown(self):
        assert identity.verdict(None, {"registered": NEW}) == identity.UNKNOWN

    def test_no_spool_is_gone(self):
        assert identity.verdict(OLD, None) == identity.GONE

    def test_a_spool_without_a_stamp_refutes_nothing(self):
        assert identity.verdict(OLD, {"registered": None}) == identity.SAME
        assert identity.stamp_of({"registered": "not a date"}) is None

    def test_usage_history_reads_stamps_through_the_same_rule(self):
        from backend.app.services.spoolman_usage_history import belongs_to

        assert belongs_to("2026-10-01T08:00:00+00:00", None, OLD) is True
        assert belongs_to(NEW, None, OLD) is False


# --- B06: only Spoolman's own 404 says "gone" -----------------------------------------


class TestSpoolmanNotFound:
    @pytest.mark.parametrize(
        ("body", "headers", "gone"),
        [
            ('{"message": "No spool with ID 12 found."}', {"content-type": "application/json"}, True),
            ("404 page not found", {"content-type": "text/plain"}, False),  # Traefik, container restarting
            ('{"detail": "Not Found"}', {"content-type": "application/json"}, False),  # an unknown route
        ],
    )
    def test_the_body_decides(self, body, headers, gone):
        assert says_spool_not_found(httpx.Response(404, text=body, headers=headers)) is gone

    async def test_a_proxy_404_is_spoolman_not_answering(self):
        def handler(request):
            return httpx.Response(404, text="404 page not found")

        client = SpoolmanClient("http://spoolman.test:7912")
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(SpoolmanUnavailableError):
            await client.get_spool(NUMBER)
        with pytest.raises(SpoolmanUnavailableError):
            await client.use_spool(NUMBER, 1.0)
        await client._client.aclose()

    async def test_spoolmans_own_404_is_not_found(self):
        def handler(request):
            return httpx.Response(404, json={"message": "No spool with ID 12 found."})

        client = SpoolmanClient("http://spoolman.test:7912")
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(SpoolmanNotFoundError):
            await client.get_spool(NUMBER)
        with pytest.raises(SpoolmanNotFoundError):
            await client.use_spool(NUMBER, 1.0)
        await client._client.aclose()


# --- B03: a writer's proof covers the number's links -----------------------------------


class TestWriterProved:
    async def test_unstamped_rows_of_the_number_adopt_the_proof(self, db_session, printer_factory):
        printer = await printer_factory()
        fc = await _calibration(db_session, printer.id)
        await _link(db_session, printer.id, fc.id, stamp=None)
        await identity.writer_proved(db_session, NUMBER, OLD, own_slot=(printer.id, 0, 0))
        await db_session.commit()
        [link] = await _rows(db_session, SpoolmanKProfile)
        assert link.spool_registered == OLD

    async def test_a_later_spool_with_the_number_does_not_resolve_the_old_links(self, db_session, printer_factory):
        printer = await printer_factory()
        fc = await _calibration(db_session, printer.id)
        await _link(db_session, printer.id, fc.id, stamp=None)
        await identity.writer_proved(db_session, NUMBER, OLD, own_slot=None)
        await db_session.commit()

        found = await resolve_spoolman_slot_kprofile(
            db_session,
            printer_id=printer.id,
            spoolman_spool_id=NUMBER,
            spool_registered=NEW,
            nozzle_diameter=0.4,
            slot_extruder=0,
        )
        assert found is None

    async def test_another_known_stamp_is_refuted_first(self, db_session, printer_factory):
        printer = await printer_factory()
        other = await printer_factory()
        fc = await _calibration(db_session, printer.id)
        await _bind(db_session, other.id, stamp=OLD)
        await _link(db_session, printer.id, fc.id, stamp=None)
        removed = await identity.writer_proved(db_session, NUMBER, NEW, own_slot=(printer.id, 0, 1))
        await db_session.commit()
        assert removed == [(other.id, 0, 0)]
        assert await _rows(db_session, SpoolmanKProfile) == []  # made for the old holder

    async def test_the_writers_own_slot_stays_for_it(self, db_session, printer_factory):
        printer = await printer_factory()
        await _bind(db_session, printer.id, stamp=OLD)
        removed = await identity.writer_proved(db_session, NUMBER, NEW, own_slot=(printer.id, 0, 0))
        await db_session.commit()
        assert removed == []
        assert len(await _rows(db_session, SpoolmanSlotAssignment)) == 1


# --- removal, and B04: not during a print's accounting ------------------------------------


class TestDropStale:
    async def test_a_refuted_binding_and_its_links_go(self, db_session, printer_factory):
        await _spoolman_on(db_session)
        printer = await printer_factory()
        fc = await _calibration(db_session, printer.id)
        await _bind(db_session, printer.id)
        await _link(db_session, printer.id, fc.id, stamp=None)
        await _link(db_session, printer.id, (await _calibration(db_session, printer.id, "GFG98")).id, stamp=NEW)

        removed = await identity.drop_stale(NUMBER, NEW, "test")
        assert removed == [(printer.id, 0, 0)]
        assert await _rows(db_session, SpoolmanSlotAssignment) == []
        # The new spool's own link stays; the unstamped one was made for the old holder.
        assert [link.spool_registered for link in await _rows(db_session, SpoolmanKProfile)] == [NEW]

    async def test_an_unstamped_row_is_not_refuted_by_a_stamp(self, db_session, printer_factory):
        await _spoolman_on(db_session)
        printer = await printer_factory()
        await _bind(db_session, printer.id, stamp=None)
        assert await identity.drop_stale(NUMBER, NEW, "test") == []
        assert len(await _rows(db_session, SpoolmanSlotAssignment)) == 1

    async def test_a_gone_spool_takes_every_row_of_the_number(self, db_session, printer_factory):
        await _spoolman_on(db_session)
        printer = await printer_factory()
        await _bind(db_session, printer.id, stamp=None)
        assert await identity.drop_stale(NUMBER, None, "test") == [(printer.id, 0, 0)]

    async def test_a_printing_printers_row_is_kept_as_its_prints_evidence(self, db_session, printer_factory):
        await _spoolman_on(db_session)
        printer = await printer_factory()
        await _bind(db_session, printer.id)
        await _printing(db_session, printer.id)
        assert await identity.drop_stale(NUMBER, NEW, "test") == []
        assert len(await _rows(db_session, SpoolmanSlotAssignment)) == 1

    async def test_that_prints_tail_removes_it(self, db_session, printer_factory):
        await _spoolman_on(db_session)
        printer = await printer_factory()
        await _bind(db_session, printer.id)
        await _printing(db_session, printer.id)
        assert await identity.drop_stale(NUMBER, NEW, "test", tail_of=printer.id) == [(printer.id, 0, 0)]

    async def test_nothing_is_removed_with_spoolman_off(self, db_session, printer_factory):
        printer = await printer_factory()
        await _bind(db_session, printer.id)
        assert await identity.drop_stale(NUMBER, NEW, "test") == []


# --- a print's tail (§5.1) ----------------------------------------------------------------


class _Spoolman:
    def __init__(self, spool: dict | None):
        self.spool = spool
        self.gets = 0

    async def get_spool(self, number):
        self.gets += 1
        if self.spool is None:
            raise SpoolmanNotFoundError("gone")
        return dict(self.spool)


class TestChargeCheck:
    async def test_a_binding_naming_another_spool_is_not_charged(self, db_session, printer_factory):
        await _spoolman_on(db_session)
        printer = await printer_factory()
        await _bind(db_session, printer.id)
        check = identity.ChargeCheck(_Spoolman({"id": NUMBER, "registered": NEW}), printer.id)

        admission = await check.admits(NUMBER, slot_stamp=OLD, tray=0, grams=12.5)
        assert admission.ok is False
        assert check.changed == [(0, 12.5, NUMBER)]
        await check.settle()
        assert await _rows(db_session, SpoolmanSlotAssignment) == []

    async def test_an_unstamped_binding_is_charged_and_adopts(self, db_session, printer_factory):
        await _spoolman_on(db_session)
        printer = await printer_factory()
        await _bind(db_session, printer.id, stamp=None)
        client = _Spoolman({"id": NUMBER, "registered": OLD})
        check = identity.ChargeCheck(client, printer.id)

        assert (await check.admits(NUMBER, slot_stamp=None, tray=0, grams=3)).ok
        assert (await check.admits(NUMBER, slot_stamp=None, tray=0, grams=2)).ok
        assert client.gets == 1  # one read per number for the whole print
        await check.settle()
        [row] = await _rows(db_session, SpoolmanSlotAssignment)
        assert row.spool_registered == OLD

    async def test_a_journal_number_is_judged_by_the_rows_naming_it(self, db_session, printer_factory):
        await _spoolman_on(db_session)
        printer = await printer_factory()
        await _bind(db_session, printer.id)
        check = identity.ChargeCheck(_Spoolman({"id": NUMBER, "registered": NEW}), printer.id)
        assert (await check.admits(NUMBER, tray=0, grams=4)).ok is False

    async def test_a_journal_number_no_row_names_is_charged_as_before(self, db_session, printer_factory):
        printer = await printer_factory()
        check = identity.ChargeCheck(_Spoolman({"id": NUMBER, "registered": NEW}), printer.id)
        assert (await check.admits(NUMBER, tray=0, grams=4)).ok is True

    async def test_spoolman_not_answering_refutes_nothing(self, db_session, printer_factory):
        printer = await printer_factory()

        class _Down:
            async def get_spool(self, number):
                raise SpoolmanUnavailableError("down")

        check = identity.ChargeCheck(_Down(), printer.id)
        assert (await check.admits(NUMBER, slot_stamp=OLD, tray=0, grams=4)).ok is True

    async def test_b04_a_binding_refuted_mid_print_still_refutes_the_journal_number(self, db_session, printer_factory):
        """The slot card saw the wrong binding during the print; the tail must still refuse it."""
        await _spoolman_on(db_session)
        printer = await printer_factory()
        await _bind(db_session, printer.id)
        await _printing(db_session, printer.id)
        assert await identity.drop_stale(NUMBER, NEW, "slot card") == []  # kept: the print's evidence

        check = identity.ChargeCheck(_Spoolman({"id": NUMBER, "registered": NEW}), printer.id)
        assert (await check.admits(NUMBER, tray=0, grams=7)).ok is False
        await check.settle()
        assert await _rows(db_session, SpoolmanSlotAssignment) == []


# --- B01 / B09: the sync ----------------------------------------------------------------------


class TestSync:
    async def test_a_hint_never_restamps_a_wrong_binding(self, db_session, printer_factory):
        await _spoolman_on(db_session)
        printer = await printer_factory()
        await _bind(db_session, printer.id)
        change = SyncChange(0, 0, NUMBER, NEW, by_tag=False)
        moved = await asyncio.wait_for(
            write_sync_results(db_session, printer_id=printer.id, read={(0, 0): NUMBER}, changes=[change], empties=[]),
            timeout=10,  # B09: never the printer's lock twice
        )
        assert moved == [(0, 0)]
        assert await _rows(db_session, SpoolmanSlotAssignment) == []

    async def test_a_hint_adopts_for_an_unstamped_row(self, db_session, printer_factory):
        printer = await printer_factory()
        await _bind(db_session, printer.id, stamp=None)
        change = SyncChange(0, 0, NUMBER, OLD, by_tag=False)
        await write_sync_results(db_session, printer_id=printer.id, read={(0, 0): NUMBER}, changes=[change], empties=[])
        [row] = await _rows(db_session, SpoolmanSlotAssignment)
        assert row.spool_registered == OLD

    async def test_the_tags_spool_with_the_same_number_restamps_the_row(self, db_session, printer_factory):
        printer = await printer_factory()
        await _bind(db_session, printer.id)
        change = SyncChange(0, 0, NUMBER, NEW, by_tag=True)
        moved = await write_sync_results(
            db_session, printer_id=printer.id, read={(0, 0): NUMBER}, changes=[change], empties=[]
        )
        assert moved == [(0, 0)]
        [row] = await _rows(db_session, SpoolmanSlotAssignment)
        assert row.spool_registered == NEW

    async def test_a_new_tag_binding_is_stamped(self, db_session, printer_factory):
        printer = await printer_factory()
        change = SyncChange(0, 1, NUMBER, NEW, by_tag=True)
        await write_sync_results(db_session, printer_id=printer.id, read={}, changes=[change], empties=[])
        [row] = await _rows(db_session, SpoolmanSlotAssignment)
        assert (row.tray_id, row.spool_registered) == (1, NEW)


# --- D2: a change of the Spoolman database ------------------------------------------------------


class TestBaseChange:
    async def _change(self, db, **kwargs):
        async with spoolman_source_changes.owning(db) as change:
            await change.decide(db, **kwargs)
            if "url" in kwargs:
                row = (await db.execute(select(Settings).where(Settings.key == "spoolman_url"))).scalar_one()
                row.value = kwargs["url"]
            await db.commit()

    async def test_a_new_address_removes_every_binding_and_link(self, db_session, printer_factory):
        await _spoolman_on(db_session)
        printer = await printer_factory()
        fc = await _calibration(db_session, printer.id)
        await _bind(db_session, printer.id)
        await _link(db_session, printer.id, fc.id)
        await self._change(db_session, url="http://other.test:7912")
        assert await _rows(db_session, SpoolmanSlotAssignment) == []
        assert await _rows(db_session, SpoolmanKProfile) == []

    async def test_a_mode_change_leaves_the_links(self, db_session, printer_factory):
        await _spoolman_on(db_session)
        printer = await printer_factory()
        fc = await _calibration(db_session, printer.id)
        await _link(db_session, printer.id, fc.id)
        await self._change(db_session, enabled="false")
        assert len(await _rows(db_session, SpoolmanKProfile)) == 1

    async def test_an_emptied_address_is_no_new_database(self, db_session, printer_factory):
        await _spoolman_on(db_session)
        printer = await printer_factory()
        await _bind(db_session, printer.id)
        await self._change(db_session, url="")
        assert len(await _rows(db_session, SpoolmanSlotAssignment)) == 1

    async def test_the_preview_counts_what_the_change_removes(self, db_session, printer_factory):
        from backend.app.api.routes.settings import spoolman_address_change_preview

        await _spoolman_on(db_session)
        printer = await printer_factory()
        fc = await _calibration(db_session, printer.id)
        await _bind(db_session, printer.id)
        await _link(db_session, printer.id, fc.id)

        same = await spoolman_address_change_preview(url="http://SPOOLMAN.test:7912/", db=db_session, _=None)
        assert same["base_change"] is False
        other = await spoolman_address_change_preview(url="http://other.test:7912", db=db_session, _=None)
        assert (other["base_change"], other["slot_bindings"], other["k_links"], other["blocked"]) == (True, 1, 1, False)


# --- confirming readers that write to Spoolman or the printer ---------------------------------------


class TestReaders:
    async def test_drying_is_not_stamped_on_another_spool(self, db_session, printer_factory, monkeypatch):
        from backend.app.services import spool_drying, spoolman

        await _spoolman_on(db_session)
        printer = await printer_factory()
        await _bind(db_session, printer.id)
        merged: list[int] = []

        class _Client:
            async def get_spool(self, number):
                return {"id": number, "registered": NEW}

            async def merge_spool_extra(self, number, fields):
                merged.append(number)

        async def _client():
            return _Client()

        monkeypatch.setattr(spoolman, "get_spoolman_client", _client)
        from backend.app.services.printer_manager import printer_manager

        monkeypatch.setattr(printer_manager, "session_still_lives", lambda _pid: True)
        record = SimpleNamespace(temp=55, hours=4)
        monkeypatch.setattr(spool_drying, "spoolman_extra", lambda _now, _record: {"dried": "x"})
        stamped = await spool_drying._stamp_spoolman(db_session, printer.id, 0, record)
        assert (stamped, merged) == (0, [])
        assert await _rows(db_session, SpoolmanSlotAssignment) == []

    async def test_the_slot_card_shows_no_spool_for_a_refuted_binding(self, db_session, printer_factory, monkeypatch):
        from backend.app.api.routes import spoolman_inventory

        await _spoolman_on(db_session)
        printer = await printer_factory()
        await _bind(db_session, printer.id)

        class _Client:
            async def get_spool(self, number):
                return {"id": number, "registered": NEW, "filament": {}}

        async def _get_client(_db, **_kw):
            return _Client()

        monkeypatch.setattr(spoolman_inventory, "_get_client", _get_client)
        answer = await spoolman_inventory.get_spoolman_slot_assignment(
            printer_id=printer.id, ams_id=0, tray_id=0, db=db_session, _=None
        )
        assert answer is None
        assert await _rows(db_session, SpoolmanSlotAssignment) == []

    async def test_the_backup_walk_offers_no_plan_for_a_refuted_binding(self, db_session, printer_factory, monkeypatch):
        from backend.app.services import ams_backup_compatibility_apply as walker, spoolman

        await _spoolman_on(db_session)
        printer = await printer_factory()
        await _bind(db_session, printer.id)

        class _Client:
            async def get_spool(self, number):
                return {"id": number, "registered": NEW, "filament": {"material": "PLA"}}

        async def _client():
            return _Client()

        monkeypatch.setattr(spoolman, "get_spoolman_client", _client)
        walk = await walker.iter_slot_projections(db_session, printer)
        assert [c for c in walk.candidates if c.source == "spoolman"] == []
        assert walk.spoolman_refuted == {NUMBER: NEW}

    async def test_a_k_resolver_call_must_name_its_spool(self):
        with pytest.raises(TypeError):
            await resolve_spoolman_slot_kprofile(
                None, printer_id=1, spoolman_spool_id=NUMBER, nozzle_diameter=0.4, slot_extruder=0
            )


# --- acceptance r1: A01-A03 ------------------------------------------------------------------------


@pytest.fixture
def tracking_sessions(monkeypatch, test_engine):
    """The tracking module bound ``async_session`` at import; give it the test database."""
    from backend.app.core import database
    from backend.app.services import spoolman_tracking

    monkeypatch.setattr(spoolman_tracking, "async_session", database.async_session)
    return spoolman_tracking


class _Recorder:
    def __init__(self, result=None, error: BaseException | None = None):
        self.calls: list[tuple] = []
        self._result = result
        self._error = error

    async def __call__(self, *args, **kwargs):
        self.calls.append(args)
        if self._error is not None:
            raise self._error
        return self._result


def _runout(number: int = NUMBER):
    from backend.app.models.print_usage_event import EVENT_RUNOUT, KIND_AUTOSWITCH

    return SimpleNamespace(
        id=1,
        event=EVENT_RUNOUT,
        kind=KIND_AUTOSWITCH,  # a closed episode: drained, and archived when that is on
        global_tray_id=0,
        layer_num=40,
        spool_id=None,
        spoolman_spool_id=number,
        archive_id=1,
    )


class TestAcceptanceR1:
    async def test_a01_the_zero_correction_neither_drains_nor_archives_another_spool(
        self, db_session, printer_factory, tracking_sessions
    ):
        await _spoolman_on(db_session)
        db_session.add(Settings(key="runout_archive_spool_enabled", value="true"))
        await db_session.commit()
        printer = await printer_factory()
        await _bind(db_session, printer.id)
        client = SimpleNamespace(
            get_spool=_Recorder({"id": NUMBER, "registered": NEW, "remaining_weight": 800.0}),
            use_spool=_Recorder(),
            update_spool=_Recorder(),
            set_spool_archived=_Recorder(),
        )
        check = identity.ChargeCheck(client, printer.id)

        await tracking_sessions._apply_runout_zero_corrections_spoolman(client, [_runout()], 1, identity=check)
        assert (client.use_spool.calls, client.set_spool_archived.calls, client.update_spool.calls) == ([], [], [])
        await check.settle()
        assert await _rows(db_session, SpoolmanSlotAssignment) == []

    async def test_a01_a_stopped_prints_tail_is_one_check_settled_after_the_zero_correction(
        self, db_session, printer_factory, tracking_sessions, monkeypatch
    ):
        from backend.app.models.active_print_spoolman import ActivePrintSpoolman

        printer = await printer_factory()
        archive = PrintArchive(
            printer_id=printer.id, filename="a.3mf", file_path="", file_size=0, print_name="a", status="failed"
        )
        db_session.add(archive)
        await db_session.commit()
        db_session.add(
            ActivePrintSpoolman(printer_id=printer.id, archive_id=archive.id, filament_usage=[], ams_trays={})
        )
        await db_session.commit()

        order: list[str] = []
        seen: list = []

        async def partial(*_a, identity=None, **_kw):
            order.append("partial")
            seen.append(identity)

        async def zero(_client, _events, _archive_id, identity=None):
            order.append("zero")
            seen.append(identity)
            return 0

        async def settle(self):
            order.append("settle")

        monkeypatch.setattr(tracking_sessions, "_report_partial_usage", partial)
        monkeypatch.setattr(tracking_sessions, "_apply_runout_zero_corrections_spoolman", zero)
        monkeypatch.setattr(tracking_sessions, "_get_spoolman_client_with_fallback", _Recorder(object()))
        monkeypatch.setattr(tracking_sessions, "_load_journal_events", _Recorder([]))
        monkeypatch.setattr(identity.ChargeCheck, "settle", settle)

        await tracking_sessions._cleanup_tracking(
            printer.id, archive.id, db_session, None, None, None, "failed", charge=SimpleNamespace(enabled=True)
        )
        assert order == ["partial", "zero", "settle"]
        assert seen[0] is not None and seen[0] is seen[1]

    async def test_a02_the_slot_card_on_a_gone_spool_removes_the_row_and_the_numbers_links(
        self, db_session, printer_factory, monkeypatch
    ):
        from backend.app.api.routes import spoolman_inventory

        await _spoolman_on(db_session)
        printer = await printer_factory()
        fc = await _calibration(db_session, printer.id)
        await _bind(db_session, printer.id, stamp=None)
        await _link(db_session, printer.id, fc.id, stamp=None)

        async def _get_client(_db, **_kw):
            return SimpleNamespace(get_spool=_Recorder(error=SpoolmanNotFoundError("gone")))

        monkeypatch.setattr(spoolman_inventory, "_get_client", _get_client)
        answer = await spoolman_inventory.get_spoolman_slot_assignment(
            printer_id=printer.id, ams_id=0, tray_id=0, db=db_session, _=None
        )
        assert answer is None
        assert await _rows(db_session, SpoolmanSlotAssignment) == []
        assert await _rows(db_session, SpoolmanKProfile) == []

    async def test_a02_during_a_print_the_gone_binding_stays_for_its_tail(
        self, db_session, printer_factory, monkeypatch
    ):
        from backend.app.api.routes import spoolman_inventory

        await _spoolman_on(db_session)
        printer = await printer_factory()
        await _bind(db_session, printer.id)
        await _printing(db_session, printer.id)

        async def _get_client(_db, **_kw):
            return SimpleNamespace(get_spool=_Recorder(error=SpoolmanNotFoundError("gone")))

        monkeypatch.setattr(spoolman_inventory, "_get_client", _get_client)
        await spoolman_inventory.get_spoolman_slot_assignment(
            printer_id=printer.id, ams_id=0, tray_id=0, db=db_session, _=None
        )
        assert len(await _rows(db_session, SpoolmanSlotAssignment)) == 1

    async def test_a03_the_remain_delta_says_a_gone_spool_too(self, db_session, printer_factory, tracking_sessions):
        await _spoolman_on(db_session)
        printer = await printer_factory()
        await _bind(db_session, printer.id)
        client = SimpleNamespace(get_spool=_Recorder(error=SpoolmanNotFoundError("gone")), use_spool=_Recorder())
        check = identity.ChargeCheck(client, printer.id)

        await tracking_sessions._report_remain_delta_for_slots(
            client,
            printer_id=printer.id,
            tray_remain_start={"0-0": {"remain": 80, "tray_uuid": ""}},
            current_lookup={"0-0": {"remain": 50, "tray_uuid": ""}},
            handled_global_tray_ids=set(),
            archive_id=1,
            identity=check,
        )
        assert client.use_spool.calls == []
        assert check.changed == [(0, 0.0, NUMBER)]
        await check.settle()
        assert await _rows(db_session, SpoolmanSlotAssignment) == []
