"""The chamber-light lease (services/camera_light): on only when off, off only when we switched it on.

Spec 60-specs/camera-light-lease-spec §8. The printer is a fake with the three
fields the lease reads (``connected``, ``chamber_light``, ``has_chamber_light``)
and a ``set_chamber_light`` that records what it was told; the printer's
answer — the ``lights_report`` — is played back through ``note_light_report``
the way printer_manager wires it. The settings' verdict is faked at
``allowed`` for the mechanism tests and read from the database in the policy
class at the bottom.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from backend.app.services import camera_light


@dataclass
class _State:
    connected: bool = True
    chamber_light: bool = False
    has_chamber_light: bool = True


@dataclass
class FakeClient:
    state: _State = field(default_factory=_State)
    commands: list[bool] = field(default_factory=list)
    accept: bool = True

    def set_chamber_light(self, on: bool) -> bool:
        self.commands.append(on)
        if self.accept:
            # The physical light follows the command; the REPORT is the test's to send.
            self.state.chamber_light = on
        return self.accept


class Sleeper:
    """``camera_light._sleep`` under test control: records the delay, waits for the gate."""

    def __init__(self):
        self.delays: list[float] = []
        self.gate = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await self.gate.wait()

    async def elapse(self) -> None:
        self.gate.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)


@pytest.fixture
def printer(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(camera_light, "_get_client", lambda pid: client if pid == 1 else None)

    async def _allowed(printer_id, purpose):
        return True

    monkeypatch.setattr(camera_light, "allowed", _allowed)
    camera_light._lights.clear()
    yield client
    camera_light._lights.clear()


@pytest.fixture
def sleeper(monkeypatch):
    s = Sleeper()
    monkeypatch.setattr(camera_light, "_sleep", s)
    return s


async def _report(printer_id: int, on: bool) -> None:
    await camera_light.note_light_report(printer_id, on)


# ------------------------------------------------------------------ the mechanism


@pytest.mark.asyncio
async def test_a_light_that_is_off_is_switched_on_confirmed_and_switched_off_after_the_grace(printer, sleeper):
    lease = await camera_light.acquire(1, "telegram")
    assert lease is not None
    assert printer.commands == [True]

    settled = asyncio.create_task(lease.settle())
    await asyncio.sleep(0)
    assert not settled.done(), "settle must wait for the printer's report"
    await _report(1, True)
    await asyncio.wait_for(settled, 1)

    camera_light.release(lease)
    await asyncio.sleep(0)
    assert sleeper.delays == [camera_light.GRACE_SECONDS]
    assert printer.commands == [True], "nothing is switched off before the grace has passed"
    await sleeper.elapse()
    assert printer.commands == [True, False]


@pytest.mark.asyncio
async def test_a_light_that_is_already_on_is_never_touched(printer, sleeper):
    printer.state.chamber_light = True
    lease = await camera_light.acquire(1, "telegram")
    assert lease is not None
    await lease.settle()  # returns at once: nothing of ours to confirm
    camera_light.release(lease)
    await asyncio.sleep(0)
    assert printer.commands == []
    assert sleeper.delays == []


@pytest.mark.asyncio
async def test_two_holders_are_one_on_and_one_off(printer, sleeper):
    first = await camera_light.acquire(1, "stream")
    second = await camera_light.acquire(1, "stream")
    assert printer.commands == [True]
    camera_light.release(first)
    await asyncio.sleep(0)
    assert sleeper.delays == [], "the light is still in use"
    camera_light.release(second)
    await asyncio.sleep(0)
    assert sleeper.delays == [camera_light.GRACE_SECONDS]
    await sleeper.elapse()
    assert printer.commands == [True, False]


@pytest.mark.asyncio
async def test_a_holder_that_comes_back_within_the_grace_keeps_the_light(printer, sleeper):
    first = await camera_light.acquire(1, "telegram")
    camera_light.release(first)
    await asyncio.sleep(0)
    pending = camera_light._lights[1].release_task
    second = await camera_light.acquire(1, "telegram")
    await asyncio.sleep(0)
    assert pending.cancelled() or pending.done()
    assert printer.commands == [True], "no second on, no off in between"
    camera_light.release(second)
    await asyncio.sleep(0)
    assert len(sleeper.delays) == 2
    await sleeper.elapse()
    assert printer.commands == [True, False]


@pytest.mark.asyncio
async def test_a_declared_hold_outlives_the_grace(printer, sleeper):
    lease = await camera_light.acquire(1, "snapshot", hold=30.0)
    camera_light.release(lease)
    await asyncio.sleep(0)
    assert sleeper.delays == [pytest.approx(30.0, abs=0.05)]


@pytest.mark.asyncio
async def test_a_hold_is_extended_by_the_latest_poll_not_shortened_by_an_earlier_one(printer, sleeper, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(camera_light, "_clock", lambda: now[0])
    first = await camera_light.acquire(1, "snapshot", hold=30.0)
    camera_light.release(first)
    now[0] = 105.0
    second = await camera_light.acquire(1, "snapshot", hold=30.0)
    camera_light.release(second)
    await asyncio.sleep(0)
    # The second poll's hold ends at 135; the first's at 130 — the later one wins.
    assert sleeper.delays[-1] == pytest.approx(30.0)


def test_hold_for_poll_is_the_cadence_plus_the_capture_margin_and_is_capped():
    assert camera_light.hold_for_poll(None) is None
    assert camera_light.hold_for_poll(5000) == pytest.approx(25.0)
    assert camera_light.hold_for_poll(60_000) == pytest.approx(80.0)
    assert camera_light.hold_for_poll(999_999) == pytest.approx(140.0)
    assert camera_light.hold_for_poll(-5) == pytest.approx(camera_light.POLL_HOLD_MARGIN_SECONDS)


# ------------------------------------------------------------------ ownership


@pytest.mark.asyncio
async def test_the_operator_switching_the_light_off_during_a_lease_ends_our_ownership(printer, sleeper):
    lease = await camera_light.acquire(1, "stream")
    await _report(1, True)  # our on, confirmed
    # Five seconds later than any command of ours: the card button, the screen, Studio.
    camera_light._lights[1].last_command_at -= camera_light.ATTRIBUTION_WINDOW_SECONDS + 1
    printer.state.chamber_light = False
    await _report(1, False)
    assert camera_light._lights[1].owned is False

    camera_light.release(lease)
    await asyncio.sleep(0)
    assert sleeper.delays == [], "nothing of ours to switch off"
    assert printer.commands == [True]


@pytest.mark.asyncio
async def test_the_operator_switching_the_light_on_before_a_lease_means_we_never_switch_it_off(printer, sleeper):
    printer.state.chamber_light = True
    lease = await camera_light.acquire(1, "stream")
    await _report(1, True)  # a report we did not command; we own nothing
    camera_light.release(lease)
    await asyncio.sleep(0)
    assert printer.commands == []


@pytest.mark.asyncio
async def test_a_report_in_the_direction_we_commanded_within_the_window_is_ours(printer, sleeper):
    lease = await camera_light.acquire(1, "telegram")
    await _report(1, True)
    assert camera_light._lights[1].owned is True
    camera_light.release(lease)
    await asyncio.sleep(0)
    await sleeper.elapse()
    assert printer.commands == [True, False]


@pytest.mark.asyncio
async def test_a_foreign_switch_while_we_wait_for_confirmation_releases_the_wait(printer):
    lease = await camera_light.acquire(1, "telegram")
    settled = asyncio.create_task(lease.settle())
    await asyncio.sleep(0)
    camera_light._lights[1].last_command_at -= camera_light.ATTRIBUTION_WINDOW_SECONDS + 1
    await _report(1, False)
    await asyncio.wait_for(settled, 1)
    camera_light.release(lease)


@pytest.mark.asyncio
async def test_the_plate_check_lights_a_chamber_the_operator_darkened_under_a_viewer(printer, sleeper):
    """Upstream cf0ba31f1: the check's reference was calibrated in light, so it lights the plate even when it
    is not the first holder — and giving it back returns the chamber to the dark its operator chose (G09
    review M1): the viewer never asked for the light, and on a kiosk wall it never leaves."""
    viewer = await camera_light.acquire(1, "stream")
    await _report(1, True)
    camera_light._lights[1].last_command_at -= camera_light.ATTRIBUTION_WINDOW_SECONDS + 1
    printer.state.chamber_light = False
    await _report(1, False)  # the operator switched it off while watching
    assert printer.commands == [True]

    check = await camera_light.acquire(1, "plate_check")
    assert printer.commands == [True, True], "the plate check lights the plate whoever else holds the lease"

    camera_light.release(check)
    await asyncio.sleep(0)
    assert sleeper.delays == [camera_light.GRACE_SECONDS], "the operator's dark chamber is due back"
    assert printer.commands == [True, True], "not before the grace: two checks in a row do not blink"
    await sleeper.elapse()
    assert printer.commands == [True, True, False], "back off although the viewer still watches"
    camera_light.release(viewer)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert printer.commands == [True, True, False], "the light is nobody's now: the viewer switches nothing"


async def _darkened_under_a_viewer(printer):
    """A viewer whose operator switched the light off while watching; the chamber is dark and nobody's."""
    viewer = await camera_light.acquire(1, "stream")
    await _report(1, True)
    camera_light._lights[1].last_command_at -= camera_light.ATTRIBUTION_WINDOW_SECONDS + 1
    printer.state.chamber_light = False
    await _report(1, False)
    return viewer


@pytest.mark.asyncio
async def test_a_light_the_operator_takes_over_after_the_check_is_not_switched_off(printer, sleeper):
    """M1, the other way: lit by the check, then switched on by its operator — theirs, and it stays."""
    viewer = await _darkened_under_a_viewer(printer)
    check = await camera_light.acquire(1, "plate_check")
    await _report(1, True)
    camera_light.note_operator_switch(1)  # "light on" from the card: no report follows, it is on already
    camera_light.release(check)
    await asyncio.sleep(0)
    assert sleeper.delays == []
    camera_light.release(viewer)
    await asyncio.sleep(0)
    assert printer.commands == [True, True]


@pytest.mark.asyncio
async def test_a_light_a_viewer_holds_on_itself_is_not_touched_by_a_check_beside_it(printer, sleeper):
    """The viewer lit the chamber and still holds it: a plate check beside it switches nothing either way."""
    viewer = await camera_light.acquire(1, "stream")
    await _report(1, True)
    check = await camera_light.acquire(1, "plate_check")
    camera_light.release(check)
    await asyncio.sleep(0)
    assert sleeper.delays == []
    assert printer.commands == [True]
    camera_light.release(viewer)


@pytest.mark.asyncio
async def test_the_dark_comes_back_only_after_the_last_of_two_checks(printer, sleeper):
    viewer = await _darkened_under_a_viewer(printer)
    first = await camera_light.acquire(1, "plate_check")
    second = await camera_light.acquire(1, "plate_check")
    camera_light.release(first)
    await asyncio.sleep(0)
    assert sleeper.delays == [], "the second check still needs the light"
    camera_light.release(second)
    await asyncio.sleep(0)
    await sleeper.elapse()
    assert printer.commands == [True, True, False]
    camera_light.release(viewer)


@pytest.mark.asyncio
async def test_a_check_inside_the_grace_keeps_the_light_and_a_joining_viewer_does_not(printer, sleeper):
    viewer = await _darkened_under_a_viewer(printer)
    check = await camera_light.acquire(1, "plate_check")
    camera_light.release(check)
    await asyncio.sleep(0)
    assert sleeper.delays == [camera_light.GRACE_SECONDS]

    again = await camera_light.acquire(1, "plate_check")  # the retry of a check: no blink
    await asyncio.sleep(0)
    assert printer.commands == [True, True]
    camera_light.release(again)
    await asyncio.sleep(0)
    assert sleeper.delays == [camera_light.GRACE_SECONDS] * 2

    second_viewer = await camera_light.acquire(1, "stream")  # joins the darkened chamber: leaves it dark
    await sleeper.elapse()
    assert printer.commands == [True, True, False]
    camera_light.release(second_viewer)
    camera_light.release(viewer)


@pytest.mark.asyncio
async def test_a_viewer_joining_a_dark_chamber_still_leaves_it_dark(printer, sleeper):
    """Only the plate check overrides the operator: a second viewer does not switch the light back on."""
    viewer = await camera_light.acquire(1, "stream")
    await _report(1, True)
    camera_light._lights[1].last_command_at -= camera_light.ATTRIBUTION_WINDOW_SECONDS + 1
    printer.state.chamber_light = False
    await _report(1, False)
    second = await camera_light.acquire(1, "stream")
    assert printer.commands == [True]
    camera_light.release(second)
    camera_light.release(viewer)


@pytest.mark.asyncio
async def test_lit_since_is_the_moment_our_light_was_confirmed(printer, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(camera_light, "_clock", lambda: now[0])
    margin = camera_light.LIT_FRAME_MARGIN_SECONDS
    assert margin > 0
    lease = await camera_light.acquire(1, "plate_check")
    assert lease.lit_since() == 100.0 + margin, "unconfirmed: nothing older than now shows our light"
    now[0] = 101.0
    await _report(1, True)
    now[0] = 130.0
    assert lease.lit_since() == 101.0 + margin, "the report is not the exposure: the margin follows it"
    camera_light.release(lease)


@pytest.mark.asyncio
async def test_lit_since_is_none_for_a_light_that_was_not_ours_to_switch(printer):
    printer.state.chamber_light = True
    lease = await camera_light.acquire(1, "plate_check")
    assert lease.lit_since() is None
    camera_light.release(lease)


@pytest.mark.asyncio
@pytest.mark.parametrize("named_by", ["id", "client"])
async def test_an_explicit_on_for_a_light_we_already_lit_hands_it_back(printer, sleeper, named_by):
    """Upstream 6954cff3e leg 5. The light is on because WE lit it; "on" from the API or a macro changes
    nothing on the printer, so no report arrives to end our ownership — and we switched the light off after
    the grace, against the command that had just asked for it."""
    lease = await camera_light.acquire(1, "stream")
    await _report(1, True)
    assert camera_light._lights[1].owned is True

    if named_by == "id":
        camera_light.note_operator_switch(1)
    else:
        camera_light.note_operator_switch(client=printer)

    camera_light.release(lease)
    await asyncio.sleep(0)
    assert sleeper.delays == [], "the light is the operator's now: nothing is armed to switch it off"
    assert printer.commands == [True]


@pytest.mark.asyncio
async def test_an_operator_switch_releases_a_capture_waiting_for_our_confirmation(printer):
    lease = await camera_light.acquire(1, "telegram")
    settled = asyncio.create_task(lease.settle())
    await asyncio.sleep(0)
    camera_light.note_operator_switch(1)
    await asyncio.wait_for(settled, 1)
    assert lease.lit_since() is None
    camera_light.release(lease)


def test_an_operator_switch_on_a_printer_nobody_leased_is_ignored():
    camera_light._lights.clear()
    camera_light.note_operator_switch(42)
    camera_light.note_operator_switch(client=object())
    camera_light.note_operator_switch()


@pytest.mark.asyncio
async def test_the_chamber_light_macro_action_hands_the_light_back(printer, sleeper):
    from backend.app.core.mqtt_macro_actions import MQTT_MACRO_ACTIONS

    lease = await camera_light.acquire(1, "stream")
    await _report(1, True)
    assert MQTT_MACRO_ACTIONS["chamber_light"].dispatch(printer, "on") is True
    assert printer.commands == [True, True]
    camera_light.release(lease)
    await asyncio.sleep(0)
    assert sleeper.delays == []


@pytest.mark.asyncio
async def test_a_report_for_a_printer_nobody_leased_is_ignored():
    camera_light._lights.clear()
    await camera_light.note_light_report(42, True)


# ------------------------------------------------------------------ nothing to do


@pytest.mark.asyncio
async def test_no_printer_id_no_client_not_connected_or_no_light_is_a_no_op(printer):
    assert await camera_light.acquire(None, "telegram") is None
    assert await camera_light.acquire(2, "telegram") is None  # no client
    printer.state.connected = False
    assert await camera_light.acquire(1, "telegram") is None
    printer.state.connected = True
    printer.state.has_chamber_light = False
    assert await camera_light.acquire(1, "telegram") is None
    assert printer.commands == []


@pytest.mark.asyncio
async def test_a_refused_command_does_not_fail_the_capture_and_leaves_nothing_to_switch_off(printer, sleeper):
    printer.accept = False
    lease = await camera_light.acquire(1, "telegram")
    assert lease is not None
    await lease.settle()  # at once: we own nothing
    camera_light.release(lease)
    await asyncio.sleep(0)
    assert printer.commands == [True]
    assert sleeper.delays == []


@pytest.mark.asyncio
async def test_an_exception_from_the_client_does_not_fail_the_capture(printer, monkeypatch):
    def boom(on):
        raise RuntimeError("mqtt gone")

    monkeypatch.setattr(printer, "set_chamber_light", boom)
    lease = await camera_light.acquire(1, "telegram")
    assert lease is not None
    await lease.settle()
    camera_light.release(lease)


@pytest.mark.asyncio
async def test_settle_gives_up_after_the_confirm_timeout_and_the_capture_goes_on(printer, monkeypatch):
    monkeypatch.setattr(camera_light, "CONFIRM_TIMEOUT_SECONDS", 0.01)
    lease = await camera_light.acquire(1, "telegram")
    await asyncio.wait_for(lease.settle(), 1)
    assert camera_light._lights[1].owned is True, "a silent printer does not cost us the ownership"
    camera_light.release(lease)


@pytest.mark.asyncio
async def test_a_settings_read_that_fails_is_a_no_op_not_an_error(printer, monkeypatch):
    async def broken(printer_id, purpose):
        raise RuntimeError("db gone")

    monkeypatch.setattr(camera_light, "allowed", broken)
    assert await camera_light.acquire(1, "telegram") is None


@pytest.mark.asyncio
async def test_shutdown_drops_the_pending_switch_off_and_leaves_the_light_as_it_is(printer, sleeper):
    lease = await camera_light.acquire(1, "telegram")
    camera_light.release(lease)
    await asyncio.sleep(0)
    await camera_light.shutdown()
    assert printer.commands == [True]
    assert camera_light._lights == {}


@pytest.mark.asyncio
async def test_a_deleted_printers_pending_switch_off_never_reaches_the_printer_that_takes_its_id(printer, sleeper):
    lease = await camera_light.acquire(1, "telegram")
    camera_light.release(lease)
    await asyncio.sleep(0)
    assert sleeper.delays == [camera_light.GRACE_SECONDS]

    camera_light.forget_printer(1)  # the printer is deleted; SQLite hands id 1 to the next one
    await sleeper.elapse()
    assert printer.commands == [True], "the successor's light is not switched off for a lease it never had"
    assert 1 not in camera_light._lights


@pytest.mark.asyncio
async def test_a_lease_still_open_when_its_printer_is_deleted_arms_nothing_when_given_back(printer, sleeper):
    lease = await camera_light.acquire(1, "stream")
    camera_light.forget_printer(1)
    camera_light.release(lease)
    await asyncio.sleep(0)
    assert sleeper.delays == []
    assert printer.commands == [True]


@pytest.mark.asyncio
async def test_held_releases_on_the_way_out_even_when_the_body_raises(printer, sleeper):
    with pytest.raises(RuntimeError):
        async with camera_light.held(1, "telegram") as lease:
            assert lease is not None
            raise RuntimeError("capture failed")
    await asyncio.sleep(0)
    assert sleeper.delays == [camera_light.GRACE_SECONDS]


# ------------------------------------------------------------------ the settings' verdict


class TestPolicy:
    """``allowed`` reads the farm toggle, the printer's own answer and the Obico sub-toggle."""

    @staticmethod
    async def _set(db_session, key: str, value: str) -> None:
        from backend.app.models.settings import Settings

        db_session.add(Settings(key=key, value=value))
        await db_session.commit()

    @pytest.mark.asyncio
    async def test_the_farm_default_is_off(self, db_session, printer_factory):
        printer = await printer_factory()
        assert printer.camera_light_auto == "inherit"
        assert await camera_light.allowed(printer.id, "telegram") is False

    @pytest.mark.asyncio
    async def test_the_farm_toggle_switches_it_on_for_an_inheriting_printer(self, db_session, printer_factory):
        printer = await printer_factory()
        await self._set(db_session, camera_light.FARM_SETTING, "true")
        assert await camera_light.allowed(printer.id, "telegram") is True

    @pytest.mark.asyncio
    async def test_the_printer_says_off_over_a_farm_that_says_on(self, db_session, printer_factory):
        printer = await printer_factory(camera_light_auto="off")
        await self._set(db_session, camera_light.FARM_SETTING, "true")
        assert await camera_light.allowed(printer.id, "telegram") is False

    @pytest.mark.asyncio
    async def test_the_farm_toggle_is_the_master_switch_even_for_a_printer_that_says_on(
        self, db_session, printer_factory
    ):
        printer = await printer_factory(camera_light_auto="on")
        assert await camera_light.allowed(printer.id, "telegram") is False
        await self._set(db_session, camera_light.FARM_SETTING, "true")
        assert await camera_light.allowed(printer.id, "telegram") is True

    @pytest.mark.asyncio
    async def test_obico_needs_its_own_yes_and_that_yes_is_the_farms(self, db_session, printer_factory):
        printer = await printer_factory()
        await self._set(db_session, camera_light.FARM_SETTING, "true")
        assert await camera_light.allowed(printer.id, "obico") is False
        await self._set(db_session, camera_light.OBICO_SETTING, "true")
        assert await camera_light.allowed(printer.id, "obico") is True

    @pytest.mark.asyncio
    async def test_the_layer_timelapse_never_takes_the_light(self, db_session, printer_factory):
        printer = await printer_factory()
        await self._set(db_session, camera_light.FARM_SETTING, "true")
        await self._set(db_session, camera_light.OBICO_SETTING, "true")
        assert await camera_light.allowed(printer.id, "layer_timelapse") is False

    @pytest.mark.asyncio
    async def test_the_finish_bank_never_takes_the_light(self, db_session, printer_factory):
        """The in-print frame banked for the finish photo runs every few layers all
        print long; lighting it flashed the chamber light on every other layer."""
        printer = await printer_factory(camera_light_auto="on")
        await self._set(db_session, camera_light.FARM_SETTING, "true")
        await self._set(db_session, camera_light.OBICO_SETTING, "true")
        assert await camera_light.allowed(printer.id, "finish_bank") is False

    @pytest.mark.asyncio
    async def test_the_plate_check_always_takes_the_light_as_it_always_did(self, db_session, printer_factory):
        """Its reference was calibrated with the light on; a check in the dark pauses prints for nothing."""
        printer = await printer_factory(camera_light_auto="off")
        assert await camera_light.allowed(printer.id, "plate_check") is True

    @pytest.mark.asyncio
    async def test_an_unknown_printer_is_a_no(self, db_session):
        await self._set(db_session, camera_light.FARM_SETTING, "true")
        assert await camera_light.allowed(999_999, "telegram") is False
