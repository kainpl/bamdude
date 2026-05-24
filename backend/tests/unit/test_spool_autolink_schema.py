"""Schema coverage for the K-profile auto-link feature (m079)."""

from backend.app.schemas.spool import SpoolKProfileResponse, SpoolUpdate


def test_spool_update_accepts_resolved_filament_id():
    s = SpoolUpdate(resolved_filament_id="GFG99")
    assert s.resolved_filament_id == "GFG99"


def test_kprofile_response_defaults_auto_linked_false():
    r = SpoolKProfileResponse(id=1, spool_id=2, printer_id=3, created_at="2026-01-01T00:00:00")
    assert r.auto_linked is False
