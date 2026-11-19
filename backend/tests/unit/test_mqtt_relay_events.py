"""The MQTT relay publishes what it documents, from every place it happens.

Three defects of one kind (upstream Bambuddy dbb560dfb, and the same in our tree): ``smart_plugs/on``
and ``off`` were published by the button alone while the schedule, the print automation, the queue
and failure detection switched plugs in silence; ``maintenance/alert`` carried 0 / 0 for the figures;
a plug without an energy counter was published as 0 kWh. Two guards keep the first from coming back.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services import smart_plug_manager as plug_module
from backend.app.services.mqtt_relay import MQTTRelayService

APP = Path(__file__).resolve().parents[2] / "app"

# Modules that ARE a plug driver: their turn_on / turn_off is the device command itself.
DRIVERS = {
    "services/tasmota.py",
    "services/homeassistant.py",
    "services/mqtt_smart_plug.py",
    "services/rest_smart_plug.py",
    "services/zigbee/driver.py",
}
# A function that calls a driver's turn_on for something that is not a plug.
NOT_A_SWITCH = {
    ("services/smart_plug_manager.py", "run_linked_scripts"): "runs Home Assistant scripts, which are not plugs",
}
# The one seam: it announces the switch AND runs the printer's linked scripts by the rule its caller
# names. A switch site that called the relay itself would announce and forget the scripts.
ANNOUNCERS = {"plug_switched"}


def _functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _called_attrs(func) -> set[str]:
    names = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
            elif isinstance(node.func, ast.Name):
                names.add(node.func.id)
    return names


def test_every_function_that_switches_a_plug_tells_the_relay():
    silent = []
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP).as_posix()
        if rel in DRIVERS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Innermost functions only: a nested helper is judged by its own body.
        for func in _functions(tree):
            calls = _called_attrs(func)
            if not calls & {"turn_on", "turn_off", "toggle"}:
                continue
            nested = [f for f in _functions(func) if f is not func and _called_attrs(f) & {"turn_on", "turn_off"}]
            if nested:
                continue
            if (rel, func.name) in NOT_A_SWITCH:
                continue
            if not calls & ANNOUNCERS:
                silent.append(f"{rel}::{func.name}")
    assert silent == [], f"these switch a plug without telling the MQTT relay: {silent}"


def test_every_relay_event_has_a_caller():
    relay_events = [
        name for name in vars(MQTTRelayService) if name.startswith("on_") and callable(getattr(MQTTRelayService, name))
    ]
    source = (
        "\n".join(path.read_text(encoding="utf-8") for path in APP.rglob("*.py") if path.name != "mqtt_relay.py")
        + "\n"
        + (APP / "services" / "mqtt_relay.py").read_text(encoding="utf-8")
    )
    # Used, not only called: the online / offline pair is picked as a reference and called a line later.
    uncalled = [name for name in relay_events if not re.search("[.]" + name + "[(,) ]", source)]
    assert uncalled == [], f"relay events nobody publishes: {uncalled}"


@pytest.fixture
def relay():
    service = MQTTRelayService()
    service.enabled = True
    service.connected = True
    service.published = []
    service._publish = lambda topic, payload, retain=False: service.published.append((topic, payload))
    return service


async def test_a_maintenance_alert_carries_its_figures(relay):
    by_hours = SimpleNamespace(
        maintenance_type_name="Lubricate rods",
        interval_type="hours",
        interval_hours=100.0,
        hours_since_maintenance=112.5,
        days_since_maintenance=40.0,
    )
    by_days = SimpleNamespace(
        maintenance_type_name="Replace filter",
        interval_type="days",
        interval_hours=30.0,
        hours_since_maintenance=5.0,
        days_since_maintenance=31.0,
    )

    await relay.on_maintenance_items_due(3, "P1S", [by_hours, by_days])

    assert [topic for topic, _ in relay.published] == ["bamdude/maintenance/alert"] * 2
    first, second = (payload for _, payload in relay.published)
    assert (first["maintenance_type"], first["current_value"], first["threshold"], first["unit"]) == (
        "Lubricate rods",
        112.5,
        100.0,
        "hours",
    )
    assert (second["current_value"], second["threshold"], second["unit"]) == (31.0, 30.0, "days")


def test_a_figure_the_plug_does_not_report_is_not_a_zero():
    reading = {"power": 12.5, "today": None, "total": "n/a"}

    assert plug_module._reading(reading, "power") == 12.5
    assert plug_module._reading(reading, "today") is None
    assert plug_module._reading(reading, "total") is None
    assert plug_module._reading(reading, "absent") is None
    assert plug_module._reading({"total": 0}, "total") == 0.0


async def test_filament_low_is_published_under_the_printers_serial(relay, monkeypatch):
    from backend.app.services.printer_manager import printer_manager

    monkeypatch.setattr(
        printer_manager, "get_printer", lambda pid: SimpleNamespace(name="A1 mini", serial_number="SERIAL1")
    )

    await relay.on_filament_low(7, "A2", 9, color="Jade White", spool_id=285)

    ((topic, payload),) = relay.published
    assert topic == "bamdude/printers/SERIAL1/filament/low"
    assert (payload["slot"], payload["remaining_percent"], payload["color"], payload["spool_id"]) == (
        "A2",
        9,
        "Jade White",
        285,
    )


async def test_filament_low_for_an_unknown_printer_publishes_nothing(relay, monkeypatch):
    from backend.app.services.printer_manager import printer_manager

    monkeypatch.setattr(printer_manager, "get_printer", lambda pid: None)

    await relay.on_filament_low(7, "A2", 9)

    assert relay.published == []


async def test_a_switched_plug_is_published_with_its_printer(relay, db_session, smart_plug_factory, printer_factory):
    printer = await printer_factory(name="Farm P1S")
    plug = await smart_plug_factory(name="Bench plug", printer_id=printer.id)
    await db_session.commit()

    await relay.on_smart_plug_switched(plug.id, False)

    ((topic, payload),) = relay.published
    assert topic == "bamdude/smart_plugs/off"
    assert (payload["plug_name"], payload["state"], payload["printer_id"], payload["printer_name"]) == (
        "Bench plug",
        "off",
        printer.id,
        "Farm P1S",
    )


async def test_a_home_assistant_script_is_not_published_as_a_plug(relay, db_session, smart_plug_factory):
    script = await smart_plug_factory(name="Lights scene", plug_type="homeassistant", ha_entity_id="script.lights")
    await db_session.commit()

    await relay.on_smart_plug_switched(script.id, True)

    assert relay.published == []


async def test_a_relay_that_is_not_connected_reads_nothing(monkeypatch):
    from backend.app.core import database

    service = MQTTRelayService()
    service.enabled = True
    service.connected = False
    opened = AsyncMock()
    monkeypatch.setattr(database, "async_session", opened)

    await service.on_smart_plug_switched(1, True)
    await service.on_reconciled_prints(1, [5])

    opened.assert_not_called()
