"""Schema coverage for the K-profile auto-link feature (m079)."""

from backend.app.schemas.spool import SpoolKProfileResponse, SpoolUpdate


def test_spool_update_accepts_filament_family_id():
    s = SpoolUpdate(filament_family_id="GFG99")
    assert s.filament_family_id == "GFG99"


def test_spool_update_ignores_the_retired_legacy_field():
    # Stale FE bundles may still send resolved_filament_id (dropped in m150);
    # Pydantic must ignore it rather than 422 the whole edit.
    s = SpoolUpdate.model_validate({"filament_family_id": "GFG99", "resolved_filament_id": "GFB00"})
    assert s.filament_family_id == "GFG99"
    assert not hasattr(s, "resolved_filament_id")


def test_kprofile_response_defaults_auto_linked_false():
    r = SpoolKProfileResponse(id=1, spool_id=2, printer_id=3, created_at="2026-01-01T00:00:00")
    assert r.auto_linked is False
