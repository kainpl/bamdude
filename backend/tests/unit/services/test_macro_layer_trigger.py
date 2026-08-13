"""Which macros a layer edge crosses.

Equality would be the obvious test and the wrong one: MQTT reports get dropped,
so a print can go from layer 48 straight to 52 without ever reporting 50.
"""

import json
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import backend.app.models.printer_location  # noqa: F401 — Printer relates to it by name
from backend.app import main
from backend.app.models.archive import PrintArchive
from backend.app.models.macro import Macro
from backend.app.models.printer import Printer
from backend.app.services import macro_trigger
from backend.app.services.archive import add_fired_layer_macro
from backend.app.services.macro_matcher import LAYER_REACHED_EVENT, find_layer_macros


def _printer(model: str = "P1S") -> Printer:
    p = Printer(name="P1", ip_address="1.2.3.4", serial_number="S1", access_code="1234", model=model)
    p.swap_mode_enabled = False
    p.swap_profile = None
    return p


def _macro(layer: int | None, *, model: str = "*", enabled: bool = True, event: str = LAYER_REACHED_EVENT) -> Macro:
    m = Macro(
        name=f"at {layer}",
        printer_models=json.dumps([model]),
        swap_mode_only=False,
        swap_profile=None,
        event=event,
        action_type="mqtt_action",
        mqtt_action="print_speed",
        mqtt_action_param="1",
        delay_seconds=0,
        gcode="",
        enabled=enabled,
    )
    m.trigger_layer = layer
    return m


class TestCrossing:
    def test_the_exact_layer_fires(self) -> None:
        assert find_layer_macros(_printer(), [_macro(50)], 49, 50) != []

    def test_a_jump_over_the_layer_still_fires(self) -> None:
        assert find_layer_macros(_printer(), [_macro(50)], 48, 52) != []

    def test_a_layer_already_behind_us_does_not_fire(self) -> None:
        assert find_layer_macros(_printer(), [_macro(50)], 50, 51) == []

    def test_a_layer_still_ahead_does_not_fire(self) -> None:
        assert find_layer_macros(_printer(), [_macro(50)], 10, 11) == []

    def test_two_macros_crossed_by_one_jump_both_fire(self) -> None:
        found = find_layer_macros(_printer(), [_macro(50), _macro(51)], 48, 52)
        assert len(found) == 2


class TestTheOrdinaryMatcherStillApplies:
    def test_a_disabled_macro_does_not_fire(self) -> None:
        assert find_layer_macros(_printer(), [_macro(50, enabled=False)], 49, 50) == []

    def test_a_macro_for_another_model_does_not_fire(self) -> None:
        assert find_layer_macros(_printer("A1"), [_macro(50, model="P1S")], 49, 50) == []

    def test_a_macro_on_another_event_does_not_fire(self) -> None:
        assert find_layer_macros(_printer(), [_macro(50, event="print_started")], 49, 50) == []

    def test_a_layerless_macro_on_this_event_is_skipped(self) -> None:
        """Validation forbids it, but a hand-edited row must not crash the parse."""
        assert find_layer_macros(_printer(), [_macro(None)], 49, 50) == []


class TestTheFiredRecord:
    """What already ran is recorded on the archive, not only in memory.

    The MQTT client is destroyed and recreated on every reconnect and its
    replacement starts at layer 0, so the first report after a mid-print
    reconnect looks like a jump from 0 and re-crosses every target behind us.
    A backend restart does the same to the in-memory guard.
    """

    def test_the_first_record_sticks(self) -> None:
        archive = PrintArchive()
        archive.extra_data = {"notes": "keep me"}

        assert add_fired_layer_macro(archive, 7) is True
        assert archive.extra_data["layer_macros_fired"] == [7]
        assert archive.extra_data["notes"] == "keep me"

    def test_recording_the_same_macro_twice_is_a_no_op(self) -> None:
        archive = PrintArchive()
        archive.extra_data = {}

        assert add_fired_layer_macro(archive, 7) is True
        assert add_fired_layer_macro(archive, 7) is False
        assert archive.extra_data["layer_macros_fired"] == [7]

    def test_a_second_macro_is_appended(self) -> None:
        archive = PrintArchive()
        archive.extra_data = None

        add_fired_layer_macro(archive, 7)
        add_fired_layer_macro(archive, 9)
        assert archive.extra_data["layer_macros_fired"] == [7, 9]


def _running_client(state: str = "RUNNING", sub_stage: int = 0) -> MagicMock:
    client = MagicMock()
    client.state.connected = True
    client.state.state = state
    client.state.mc_print_sub_stage = sub_stage
    return client


def _manager(client) -> MagicMock:
    pm = MagicMock()
    pm.get_client.return_value = client
    return pm


@pytest.fixture
def dispatched(monkeypatch):
    """Collect what would have been spawned instead of running it."""
    calls: list[str] = []
    monkeypatch.setattr(
        macro_trigger,
        "spawn_background_task",
        lambda coro, name=None: (coro.close(), calls.append(name))[1],
    )
    macro_trigger._fired_layer_macros.clear()
    main._active_macro_selection.clear()
    yield calls
    macro_trigger._fired_layer_macros.clear()
    main._active_macro_selection.clear()


async def _a_layer_macro(db, printer_id: int, layer: int = 50) -> int:
    macro = Macro(
        name=f"silent from {layer}",
        printer_models=json.dumps(["*"]),
        swap_mode_only=False,
        swap_profile=None,
        event=LAYER_REACHED_EVENT,
        action_type="mqtt_action",
        mqtt_action="print_speed",
        mqtt_action_param="1",
        trigger_layer=layer,
        delay_seconds=0,
        gcode="",
        enabled=True,
        is_custom=True,
    )
    db.add(macro)
    await db.commit()
    await db.refresh(macro)
    return macro.id


@pytest.mark.asyncio
class TestFiring:
    async def test_the_gate_holds_while_not_running(self, test_engine, db_session, printer_factory, dispatched):
        printer = await printer_factory(name="P1", model="P1S")
        await _a_layer_macro(db_session, printer.id)
        factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

        await macro_trigger.fire_layer_macros(printer.id, 50, 49, factory, _manager(_running_client("PREPARE")))

        assert dispatched == []

    async def test_the_gate_holds_during_calibration(self, test_engine, db_session, printer_factory, dispatched):
        """A P1S ticks layer_num during pre-print calibration, ~30 min early."""
        printer = await printer_factory(name="P1", model="P1S")
        await _a_layer_macro(db_session, printer.id)
        factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

        await macro_trigger.fire_layer_macros(printer.id, 50, 49, factory, _manager(_running_client(sub_stage=2)))

        assert dispatched == []

    async def test_a_crossing_fires_once(self, test_engine, db_session, printer_factory, dispatched):
        printer = await printer_factory(name="P1", model="P1S")
        macro_id = await _a_layer_macro(db_session, printer.id)
        factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        pm = _manager(_running_client())
        # Macros are opt-in per print — without a selection nothing fires at all,
        # which is a different question from the one these tests ask.
        main.register_macro_selection(printer.id, {"selected_macro_ids": [macro_id]})

        await macro_trigger.fire_layer_macros(printer.id, 50, 49, factory, pm)

        assert dispatched == [f"macro-layer-{macro_id}"]

    async def test_a_reconnect_replaying_from_zero_does_not_refire(
        self, test_engine, db_session, printer_factory, dispatched
    ):
        """The replacement client's layer counter starts at 0, so the next
        report crosses the target a second time."""
        printer = await printer_factory(name="P1", model="P1S")
        macro_id = await _a_layer_macro(db_session, printer.id)
        factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        pm = _manager(_running_client())
        main.register_macro_selection(printer.id, {"selected_macro_ids": [macro_id]})

        await macro_trigger.fire_layer_macros(printer.id, 50, 49, factory, pm)
        await macro_trigger.fire_layer_macros(printer.id, 62, 0, factory, pm)

        assert len(dispatched) == 1

    async def test_clearing_the_guard_lets_the_next_print_fire_again(
        self, test_engine, db_session, printer_factory, dispatched
    ):
        printer = await printer_factory(name="P1", model="P1S")
        macro_id = await _a_layer_macro(db_session, printer.id)
        factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        pm = _manager(_running_client())
        main.register_macro_selection(printer.id, {"selected_macro_ids": [macro_id]})

        await macro_trigger.fire_layer_macros(printer.id, 50, 49, factory, pm)
        macro_trigger.clear_fired_layer_macros(printer.id)
        await macro_trigger.fire_layer_macros(printer.id, 50, 49, factory, pm)

        assert len(dispatched) == 2
