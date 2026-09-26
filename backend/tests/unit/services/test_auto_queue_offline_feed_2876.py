"""The auto-queue does not switch on a printer that cannot run the job
(upstream dd50c51c, #2876).

The wake step chose a printer by model alone. With every matching printer off,
it switched on the first one with an Auto On plug, and only then — once the
printer reported — did routing read what it had loaded; a job for a colour
loaded at the far end of the farm woke every earlier printer in turn.

What a switched-off printer holds is readable while it is off, from two
places (the owner's ruling, 2026-09-26):

- a slot bound to a spool — BamDude's own or a Spoolman one — is that spool:
  the assigned inventory is the truth, and it survives a restart;
- an unbound slot is what the printer last reported in this process: a
  configured type and colour, or nothing the matcher could use;
- with no reading at all (BamDude restarted while the printer was off) a
  slot is unknown, and an unknown printer is woken — never having heard is
  not the same as nothing being loaded.

A printer is passed over only when every slot the job may draw from is known
and none gives a channel its material (base-type equivalence, as routing
reads it) or its forced colour. Nozzles, profile ids and distinct sources are
not judged here: those are routing's questions once the printer is up, and a
wrong "no" would strand the job with no printer ever woken for it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.services.filament_requirements import PrintRequirements
from backend.app.services.filament_routing import RoutingPolicy
from backend.app.services.offline_feed import OfflineFeed, OfflineSource, feed_from, offline_shortfall
from backend.app.services.printer_manager import PrinterManager


def _req(*slots):
    return PrintRequirements(
        status="ok",
        model="P1S",
        resolved_plate_id=1,
        used_filaments=tuple(
            {"slot_id": i, "type": t, "color": c, "tray_info_idx": None, "used_grams": 1.0, "nozzle_id": None}
            for i, (t, c) in enumerate(slots, 1)
        ),
    )


def _tray(tid, tray_type="", color="", **extra):
    return {"id": str(tid), "tray_type": tray_type, "tray_color": color, **extra}


def _reading(trays, vt=None):
    return {
        "ams": [{"id": "0", "tray": trays}],
        "vt_tray": vt if vt is not None else [{"id": "254", "tray_type": "", "tray_color": ""}],
    }


def _feed(*sources, complete=True):
    return OfflineFeed(
        sources=tuple(OfflineSource(k, m, c) for k, m, c in sources), ams_known=complete, external_known=complete
    )


class TestWhatAnOffPrinterHolds:
    def test_a_bound_slot_is_its_spool_whatever_the_reading_says(self):
        feed = feed_from(_reading([_tray(0, "PLA", "FFFFFFFF")]), {(0, 0): (("PETG",), "FF0000FF")})
        assert feed.sources == (OfflineSource("ams", "PETG", "FF0000FF"),)

    def test_a_bound_spool_is_every_type_its_slot_can_show(self):
        # A spool saying PLA whose family is PLA Aero is written as PLA-AERO:
        # the material column alone is not what routing will compare.
        feed = feed_from(_reading([_tray(0)]), {(0, 0): (("PLA", "PLA-AERO"), "FFFFFFFF")})
        assert offline_shortfall(_req(("PLA-AERO", None)), RoutingPolicy(), feed) == []

    def test_an_unbound_slot_is_the_last_reading(self):
        feed = feed_from(_reading([_tray(0, "PLA", "00FF00FF"), _tray(1)]), {})
        assert feed.sources == (OfflineSource("ams", "PLA", "00FF00FF"),)
        assert feed.ams_known and feed.external_known

    def test_the_external_holder_is_keyed_as_the_assignments_are(self):
        # An external assignment is ams_id 255, tray 0/1 ↔ vt_tray 254/255.
        feed = feed_from(_reading([], vt=[{"id": "254", "tray_type": ""}]), {(255, 0): (("ABS",), "000000FF")})
        assert feed.sources == (OfflineSource("external", "ABS", "000000FF"),)

    def test_an_ht_slot_matches_its_assignment_whatever_id_it_reports(self):
        reading = {"ams": [{"id": "128", "tray": [_tray(4, "PLA", "FFFFFFFF")]}], "vt_tray": []}
        feed = feed_from(reading, {(128, 0): (("ASA",), "FFFFFFFF")})
        assert feed.sources == (OfflineSource("ams", "ASA", "FFFFFFFF"),)

    def test_no_reading_leaves_the_rest_unknown(self):
        feed = feed_from({}, {(0, 0): (("PLA",), "FFFFFFFF")})
        assert feed.sources == (OfflineSource("ams", "PLA", "FFFFFFFF"),)
        assert not feed.ams_known and not feed.external_known

    def test_a_bound_spool_that_could_not_be_read_is_unknown(self):
        # Spoolman down: the slot HAS a spool, we just cannot say which.
        feed = feed_from(_reading([_tray(0, "PLA", "FFFFFFFF")]), {(0, 0): None})
        assert not feed.ams_known
        assert feed.external_known

    def test_a_reading_without_the_external_holder_leaves_it_unknown(self):
        feed = feed_from({"ams": [{"id": "0", "tray": [_tray(0, "PLA")]}]}, {})
        assert feed.ams_known and not feed.external_known


class TestWhenItIsPassedOver:
    def test_no_known_source_of_the_material(self):
        missing = offline_shortfall(_req(("PETG", "FF0000FF")), RoutingPolicy(), _feed(("ams", "PLA", "FF0000FF")))
        assert missing == [{"slot": 1, "wanted": "PETG"}]

    def test_base_type_equivalence_is_routings_own(self):
        # PA-CF / PA12-CF / PAHT-CF are one group for routing, so for this too.
        assert offline_shortfall(_req(("PA12-CF", None)), RoutingPolicy(), _feed(("ams", "PA-CF", None))) == []

    def test_a_forced_colour_that_is_not_loaded(self):
        policy = RoutingPolicy(force_color_match=True)
        missing = offline_shortfall(_req(("PLA", "FF0000FF")), policy, _feed(("ams", "PLA", "00FF00FF")))
        assert missing == [{"slot": 1, "wanted": "PLA (#FF0000)"}]

    def test_a_preferred_colour_is_not_a_reason(self):
        assert offline_shortfall(_req(("PLA", "FF0000FF")), RoutingPolicy(), _feed(("ams", "PLA", "00FF00FF"))) == []

    def test_an_override_speaks_for_its_channel(self):
        policy = RoutingPolicy(filament_overrides=({"slot_id": 1, "type": "PETG", "color": "0000FFFF"},))
        assert offline_shortfall(_req(("PLA", None)), policy, _feed(("ams", "PLA", None))) == [
            {"slot": 1, "wanted": "PETG"}
        ]

    def test_the_feed_policy_limits_which_holders_count(self):
        feed = _feed(("external", "PETG", None))
        assert offline_shortfall(_req(("PETG", None)), RoutingPolicy(feed_policy="ams_only"), feed) == [
            {"slot": 1, "wanted": "PETG"}
        ]
        assert offline_shortfall(_req(("PETG", None)), RoutingPolicy(), feed) == []


class TestWhenItIsWokenAnyway:
    def test_an_unknown_slot(self):
        feed = OfflineFeed(sources=(OfflineSource("ams", "PLA", None),), ams_known=False, external_known=True)
        assert offline_shortfall(_req(("PETG", None)), RoutingPolicy(), feed) == []

    def test_only_the_holders_the_policy_uses_must_be_known(self):
        feed = OfflineFeed(sources=(OfflineSource("ams", "PLA", None),), ams_known=True, external_known=False)
        assert offline_shortfall(_req(("PETG", None)), RoutingPolicy(feed_policy="ams_only"), feed) == [
            {"slot": 1, "wanted": "PETG"}
        ]
        assert offline_shortfall(_req(("PETG", None)), RoutingPolicy(), feed) == []

    @pytest.mark.parametrize(
        "policy",
        [RoutingPolicy(review_required=True), RoutingPolicy(mode="pinned")],
    )
    def test_a_job_routing_itself_cannot_judge(self, policy):
        assert offline_shortfall(_req(("PETG", None)), policy, _feed(("ams", "PLA", None))) == []

    def test_unreadable_requirements(self):
        req = PrintRequirements(status="unavailable", reason="source_unreadable")
        assert offline_shortfall(req, RoutingPolicy(), _feed(("ams", "PLA", None))) == []

    def test_a_forced_colour_the_file_does_not_name(self):
        policy = RoutingPolicy(force_color_match=True)
        assert offline_shortfall(_req(("PLA", None)), policy, _feed(("ams", "PLA", "00FF00FF"))) == []


class TestTheReadingOutlivesTheClient:
    """Dropping a client drops its state, and every reconnect drops one — the
    record is kept beside the clients, never merged back into live status."""

    @staticmethod
    def _client(raw):
        return SimpleNamespace(state=SimpleNamespace(raw_data=raw), disconnect=lambda timeout=0: None)

    def test_a_dropped_client_leaves_its_trays_behind(self):
        pm = PrinterManager()
        raw = _reading([_tray(0, "PLA", "FFFFFFFF")])
        pm._clients[7] = self._client(raw)
        pm.disconnect_printer(7)
        assert pm.last_tray_reading(7) == raw

    def test_a_live_reading_wins(self):
        pm = PrinterManager()
        pm._last_trays[7] = _reading([_tray(0, "PLA")])
        live = _reading([_tray(0, "PETG")])
        pm._clients[7] = self._client(live)
        assert pm.last_tray_reading(7) == live

    def test_a_client_that_never_reported_falls_back_to_the_record(self):
        pm = PrinterManager()
        pm._last_trays[7] = _reading([_tray(0, "PLA")])
        pm._clients[7] = self._client({})
        assert pm.last_tray_reading(7) == _reading([_tray(0, "PLA")])

    def test_never_heard_is_empty(self):
        assert PrinterManager().last_tray_reading(7) == {}


# --- Where it is asked -------------------------------------------------------


class _Feeds:
    """An OfflineFeedCache stand-in: one feed per printer id."""

    def __init__(self, feeds):
        self.feeds = feeds
        self.asked: list[int] = []

    async def get(self, db, printer_id):
        self.asked.append(printer_id)
        return self.feeds[printer_id]


def _item(**over):
    base = {
        "id": 1,
        "target_model": "P1S",
        "require_previous_success": False,
        "plate_id": None,
        "filament_overrides": None,
        "use_ams": True,
        "force_color_match": False,
        "feed_policy": None,
        "allow_base_material_match": True,
        "waiting_reason": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _off_snapshot(pid):
    from backend.app.services.printer_feed_snapshot import PrinterFeedSnapshot

    return PrinterFeedSnapshot(pid, "P1S", False, 0, "", False, False, False, ())


@pytest.mark.asyncio
class TestTheWaitingReasonSaysWhy:
    async def _reason(self, feeds, req):
        from unittest.mock import AsyncMock, patch

        from backend.app.services import auto_queue_eligibility as elig

        printers = [SimpleNamespace(id=pid, name=f"P{pid}") for pid in feeds.feeds]
        with (
            patch.object(elig, "printers_for_item", AsyncMock(return_value=(printers, "P1S", ""))),
            patch.object(elig, "read_item_requirements", AsyncMock(return_value=req)),
            patch.object(elig.printer_manager, "get_feed_snapshot", side_effect=_off_snapshot),
        ):
            result = await elig.find_eligible_printer(None, _item(), set(), offline_feeds=feeds)
        return result

    async def test_an_off_printer_that_cannot_run_it_is_named_with_what_it_lacks(self):
        feeds = _Feeds({1: _feed(("ams", "PLA", "FFFFFFFF"))})
        result = await self._reason(feeds, _req(("PETG", None)))
        assert result.printer is None
        assert result.reason.startswith("P1: ")
        assert "PETG" in result.reason
        assert "offline" not in result.reason.lower()

    async def test_one_that_may_still_run_it_stays_offline(self):
        feeds = _Feeds({1: _feed(("ams", "PETG", None))})
        result = await self._reason(feeds, _req(("PETG", None)))
        assert "offline" in result.reason.lower()


@pytest.mark.asyncio
class TestTheWakeStepPassesItOver:
    async def _wake(self, db_session, printer_factory, smart_plug_factory, feeds, req):
        from unittest.mock import AsyncMock, patch

        from backend.app.services import auto_queue_scheduler as aqs

        printers = []
        for pid in feeds.feeds:
            printer = await printer_factory(name=f"P{pid}", model="P1S")
            await smart_plug_factory(name=f"plug{printer.id}", printer_id=printer.id, auto_on=True)
            printers.append(printer)
        feeds.feeds = {p.id: feed for p, feed in zip(printers, feeds.feeds.values(), strict=False)}
        turned_on: list[int] = []

        async def _service(plug, db):
            async def turn_on(p):
                turned_on.append(p.printer_id)

            return SimpleNamespace(turn_on=turn_on)

        sched = aqs.AutoQueueScheduler()
        sched._wake_cooldowns = {}
        with (
            patch.object(aqs, "offline_candidates_for", AsyncMock(return_value=printers)),
            patch(
                "backend.app.services.smart_plug_manager.smart_plug_manager.get_service_for_plug",
                side_effect=_service,
            ),
        ):
            woke = await sched._wake_offline_printer(db_session, _item(), set(), requirements=req, offline_feeds=feeds)
        return woke, turned_on, printers

    async def test_the_first_printer_that_can_run_it_is_woken(self, db_session, printer_factory, smart_plug_factory):
        feeds = _Feeds({1: _feed(("ams", "PLA", None)), 2: _feed(("ams", "PETG", None))})
        woke, turned_on, printers = await self._wake(
            db_session, printer_factory, smart_plug_factory, feeds, _req(("PETG", None))
        )
        assert woke is True
        assert turned_on == [printers[1].id]

    async def test_none_is_woken_when_none_can(self, db_session, printer_factory, smart_plug_factory):
        feeds = _Feeds({1: _feed(("ams", "PLA", None)), 2: _feed(("ams", "ABS", None))})
        woke, turned_on, _ = await self._wake(
            db_session, printer_factory, smart_plug_factory, feeds, _req(("PETG", None))
        )
        assert woke is False
        assert turned_on == []

    async def test_an_unknown_printer_is_woken(self, db_session, printer_factory, smart_plug_factory):
        feeds = _Feeds({1: OfflineFeed()})
        woke, turned_on, printers = await self._wake(
            db_session, printer_factory, smart_plug_factory, feeds, _req(("PETG", None))
        )
        assert woke is True
        assert turned_on == [printers[0].id]
