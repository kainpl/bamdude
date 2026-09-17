"""``filament_low`` for the slots usage tracking cannot see — one announcement per spool per slot.

Bound-to-BamDude spools warn from ``usage_tracker._warn_if_low_stock`` (m117);
these pin what the AMS-sync check adds: a Spoolman-bound slot from remaining
over initial weight, a BamDude-bound slot deliberately skipped, an UNBOUND slot
silent whatever the printer's counter says, the inventory's own threshold
(clamped), hysteresis, re-arming on a new spool, forgetting an emptied slot,
and never raising into the sync.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services import filament_low


def _status(trays: list[dict], vt: list[dict] | None = None):
    """A PrinterState-shaped object: one AMS unit with the given trays."""
    return SimpleNamespace(raw_data={"ams": [{"id": 0, "tray": trays}], "vt_tray": vt or [], "ams_extruder_map": {}})


def _tray(tray_id: int, remain: int = -1, tray_type: str = "PLA", color: str = "FF0000FF", uuid: str = "") -> dict:
    return {"id": tray_id, "tray_type": tray_type, "tray_color": color, "remain": remain, "tray_uuid": uuid}


def _spoolman(**slots: tuple[float, float | None]):
    """Spoolman bindings: ``s<gtid>=(remaining_grams, initial_grams)``."""
    grams = {int(k[1:]): r for k, (r, _) in slots.items()}
    labels = {int(k[1:]): lab for k, (_, lab) in slots.items() if lab is not None}
    return True, grams, labels


@pytest.fixture(autouse=True)
def _clean_memory():
    filament_low._announced.clear()
    yield
    filament_low._announced.clear()


def _patch(monkeypatch, *, threshold: float = 20.0, bindings=(False, {}, {})):
    monkeypatch.setattr(filament_low, "read_threshold", AsyncMock(return_value=threshold))
    monkeypatch.setattr(filament_low, "_bindings", AsyncMock(return_value=bindings))


@pytest.mark.asyncio
@pytest.mark.parametrize("remain", [12, 0, -1])
async def test_an_unbound_slot_says_nothing_whatever_the_printer_counts(monkeypatch, remain):
    """The printer's ``remain`` is never a source (ruling 2026-09-17): a spool
    without an RFID tag — every third-party spool, everything on the external
    holder — reports 0, and read as a percent that announced a whole farm as
    empty after every restart (14 rows, 7 per restart, on a farm with filament
    on every one of them). What BamDude knows about a slot is what is ASSIGNED
    to it; an unbound slot is unknown, not empty."""
    _patch(monkeypatch)
    ext = {"id": 254, "tray_type": "PLA", "tray_color": "000000FF", "remain": remain}
    assert await filament_low.evaluate(None, 1, _status([_tray(0, remain=remain, uuid="abc")], vt=[ext])) == []
    assert filament_low._announced == {}


@pytest.mark.asyncio
async def test_a_bamdude_bound_spool_is_left_to_usage_tracking(monkeypatch):
    """This slot is bound to BamDude's own inventory, whose consumption write
    already warns with a persisted memory. Not a second announcer."""
    _patch(monkeypatch, bindings=(False, {0: 40.0}, {}))
    assert await filament_low.evaluate(None, 1, _status([_tray(0, remain=5, uuid="u1")])) == []
    assert (1, 0) not in filament_low._announced


@pytest.mark.asyncio
async def test_a_spoolman_bound_slot_uses_remaining_over_initial_weight(monkeypatch):
    # The printer says 80 %, Spoolman says 90 g of a 1000 g spool.
    _patch(monkeypatch, bindings=_spoolman(s0=(90.0, 1000.0)))
    low = await filament_low.evaluate(None, 1, _status([_tray(0, remain=80)]))
    assert [(s.label, s.percent) for s in low] == [("A1", 9)]


@pytest.mark.asyncio
async def test_a_spoolman_bound_slot_without_a_known_weight_says_nothing(monkeypatch):
    _patch(monkeypatch, bindings=_spoolman(s0=(90.0, None)))
    assert await filament_low.evaluate(None, 1, _status([_tray(0, remain=5)])) == []


@pytest.mark.asyncio
async def test_it_announces_once_and_re_arms_only_above_the_hysteresis(monkeypatch):
    st = _status([_tray(0, uuid="u1")])  # the printer's counter stays -1 throughout

    async def at(grams: float):
        _patch(monkeypatch, threshold=15.0, bindings=_spoolman(s0=(grams, 1000.0)))
        return await filament_low.evaluate(None, 1, st)

    assert len(await at(100.0)) == 1  # 10 %
    assert await at(100.0) == []  # same spool, still low: silent
    # 17 %: above the threshold but inside the hysteresis band — still armed-off.
    assert await at(170.0) == []
    assert await at(120.0) == []
    # 20 % (threshold + hysteresis): forgotten, so the next drop announces again.
    assert await at(200.0) == []
    assert len(await at(120.0)) == 1


@pytest.mark.asyncio
async def test_a_new_spool_in_the_same_slot_announces_again(monkeypatch):
    _patch(monkeypatch, bindings=_spoolman(s0=(100.0, 1000.0)))
    assert len(await filament_low.evaluate(None, 1, _status([_tray(0, uuid="u1")]))) == 1
    assert len(await filament_low.evaluate(None, 1, _status([_tray(0, uuid="u2")]))) == 1


@pytest.mark.asyncio
async def test_an_emptied_slot_is_forgotten(monkeypatch):
    _patch(monkeypatch, bindings=_spoolman(s0=(100.0, 1000.0)))
    assert len(await filament_low.evaluate(None, 1, _status([_tray(0, uuid="u1")]))) == 1
    assert await filament_low.evaluate(None, 1, _status([])) == []
    assert (1, 0) not in filament_low._announced
    assert len(await filament_low.evaluate(None, 1, _status([_tray(0, uuid="u1")]))) == 1


@pytest.mark.asyncio
async def test_a_slot_whose_binding_went_away_is_forgotten(monkeypatch):
    """Unassigning the spool re-arms the slot: the next spool assigned there is news."""
    _patch(monkeypatch, bindings=_spoolman(s0=(100.0, 1000.0)))
    assert len(await filament_low.evaluate(None, 1, _status([_tray(0, uuid="u1")]))) == 1
    _patch(monkeypatch)
    assert await filament_low.evaluate(None, 1, _status([_tray(0, uuid="u1")])) == []
    assert (1, 0) not in filament_low._announced


@pytest.mark.asyncio
async def test_zero_threshold_turns_the_event_off(monkeypatch):
    _patch(monkeypatch, threshold=0.0, bindings=_spoolman(s0=(10.0, 1000.0)))
    assert await filament_low.evaluate(None, 1, _status([_tray(0, uuid="u1")])) == []


@pytest.mark.asyncio
async def test_printers_do_not_share_memory(monkeypatch):
    _patch(monkeypatch, bindings=_spoolman(s0=(100.0, 1000.0)))
    assert len(await filament_low.evaluate(None, 1, _status([_tray(0, uuid="u1")]))) == 1
    assert len(await filament_low.evaluate(None, 2, _status([_tray(0, uuid="u1")]))) == 1


@pytest.mark.asyncio
async def test_check_printer_sends_the_event_with_the_slot_label_and_percent(monkeypatch):
    _patch(monkeypatch, bindings=_spoolman(s2=(70.0, 1000.0)))
    sent = AsyncMock()
    monkeypatch.setattr(
        "backend.app.services.notification_service.notification_service.on_filament_low", sent, raising=True
    )
    monkeypatch.setattr(
        "backend.app.services.printer_manager.printer_manager.get_status",
        lambda pid: _status([_tray(2, uuid="u9", color="00FF00FF")]),
        raising=True,
    )
    monkeypatch.setattr(
        "backend.app.services.printer_manager.printer_manager.get_printer",
        lambda pid: SimpleNamespace(name="A1-01"),
        raising=True,
    )
    assert await filament_low.check_printer(None, 5) == 1
    args, kwargs = sent.await_args
    assert args[0] == 5 and args[1] == "A1-01" and args[2] == "A3" and args[3] == 7
    assert kwargs.get("color") == "#00FF00"


@pytest.mark.asyncio
async def test_check_printer_never_raises_into_the_sync(monkeypatch):
    monkeypatch.setattr(filament_low, "evaluate", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr("backend.app.services.printer_manager.printer_manager.get_status", lambda pid: None)
    monkeypatch.setattr("backend.app.services.printer_manager.printer_manager.get_printer", lambda pid: None)
    assert await filament_low.check_printer(None, 5) == 0


@pytest.mark.asyncio
async def test_the_threshold_is_the_inventorys_own_and_is_clamped(monkeypatch):
    values = iter([40.0, 250.0, -3.0, "garbage"])
    monkeypatch.setattr(filament_low, "_global_low_stock_threshold", AsyncMock(side_effect=lambda db: next(values)))
    assert await filament_low.read_threshold(None) == 40.0
    assert await filament_low.read_threshold(None) == 100.0
    assert await filament_low.read_threshold(None) == 0.0
    assert await filament_low.read_threshold(None) == 0.0
