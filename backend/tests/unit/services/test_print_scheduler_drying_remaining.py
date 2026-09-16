"""``drying_remaining_seconds`` answers what the queue gate reads — a cycle of
OURS, timed by the AMS countdown — and nothing else (vault
60-specs/farm-forecast-v2-spec §6)."""

from types import SimpleNamespace

from backend.app.services import print_scheduler as ps


def _status(*minutes):
    return SimpleNamespace(raw_data={"ams": [{"dry_time": m} for m in minutes]})


def test_drying_remaining_seconds_reads_the_gate_it_mirrors(monkeypatch):
    scheduler = ps.PrintScheduler()
    monkeypatch.setattr(ps.printer_manager, "get_status", lambda pid: _status(30, 45))
    assert scheduler.drying_remaining_seconds(1) == 0  # the AMS is drying, but not on our say-so
    scheduler._drying_in_progress[1] = 1.0
    assert scheduler.drying_remaining_seconds(1) == 45 * 60  # the longest unit
    monkeypatch.setattr(ps.printer_manager, "get_status", lambda pid: _status(0))
    assert scheduler.drying_remaining_seconds(1) == 0
    monkeypatch.setattr(ps.printer_manager, "get_status", lambda pid: None)
    assert scheduler.drying_remaining_seconds(1) == 0
