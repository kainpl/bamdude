"""The objects endpoint carries each marker's position.

Placement moved to the server so the web overlay and the Telegram bot's
rendered image cannot disagree about where an object is. This pins the
contract the two of them read.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _client_state(objects: dict, *, bbox=None, approximate=False, skipped=()):
    state = MagicMock()
    state.printable_objects = objects
    state.printable_objects_bbox_all = bbox
    state.printable_objects_approximate = approximate
    state.skipped_objects = list(skipped)
    state.state = "RUNNING"
    state.skip_objects_supported = True
    return state


async def _objects(monkeypatch, db_session, printer_factory, state) -> dict:
    """Call the endpoint directly, with its two dependencies supplied by hand.

    ⚠️ ``ensure_fresh_connection_for_printer`` is stubbed to True: it reaches
    for real MQTT, and this test is about the shape of the answer rather than
    about reconnecting."""
    from unittest.mock import AsyncMock

    from backend.app.api.routes import printers as route

    printer = await printer_factory()
    client = MagicMock()
    client.state = state
    monkeypatch.setattr(route.printer_manager, "get_client", lambda _pid: client)
    monkeypatch.setattr(route.printer_manager, "ensure_fresh_connection_for_printer", AsyncMock(return_value=True))
    return await route.get_printable_objects(printer.id, db=db_session)


async def test_every_object_carries_a_marker(monkeypatch, db_session, printer_factory):
    payload = await _objects(
        monkeypatch,
        db_session,
        printer_factory,
        _client_state(
            {
                1: {"name": "a", "x": 0.25, "y": 0.25, "norm": True},
                2: {"name": "b", "x": 0.75, "y": 0.75, "norm": True},
            },
        ),
    )

    assert payload["total"] == 2
    for obj in payload["objects"]:
        assert set(obj["marker"]) == {"x", "y"}, obj


async def test_the_marker_matches_the_shared_placement(monkeypatch, db_session, printer_factory):
    """Not recomputed here — the endpoint must call the one implementation."""
    from backend.app.services.plate_markers import marker_position

    state = _client_state({7: {"name": "part", "x": 100.0, "y": 50.0}}, bbox=[0, 0, 200, 200])

    payload = await _objects(monkeypatch, db_session, printer_factory, state)

    assert payload["objects"][0]["marker"] == marker_position({"x": 100.0, "y": 50.0}, 0, 1, [0, 0, 200, 200])


async def test_objects_without_coordinates_still_get_one(monkeypatch, db_session, printer_factory):
    """⚠️ The grid fallback. Meaningless as a position, essential as a target:
    without a marker the object cannot be pressed at all."""
    payload = await _objects(
        monkeypatch, db_session, printer_factory, _client_state({1: "legacy name", 2: "another"}, approximate=True)
    )

    markers = [o["marker"] for o in payload["objects"]]
    assert len(markers) == 2
    assert markers[0] != markers[1], "two objects landed on the same spot"
    assert payload["positions_approximate"] is True


async def test_the_bbox_is_still_served(monkeypatch, db_session, printer_factory):
    payload = await _objects(
        monkeypatch, db_session, printer_factory, _client_state({1: {"name": "a"}}, bbox=[0, 0, 200, 200])
    )

    assert payload["bbox_all"] == [0, 0, 200, 200]


async def test_skipped_state_survives_alongside_the_marker(monkeypatch, db_session, printer_factory):
    payload = await _objects(
        monkeypatch, db_session, printer_factory, _client_state({1: {"name": "a"}, 2: {"name": "b"}}, skipped=[1])
    )

    by_id = {o["id"]: o for o in payload["objects"]}
    assert by_id[1]["skipped"] is True
    assert by_id[2]["skipped"] is False
    assert by_id[1]["marker"], "a skipped object still needs a position — it stays on the plate picture"
