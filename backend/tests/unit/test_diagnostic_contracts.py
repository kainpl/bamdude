"""The diagnostic boundary is strict and its cursors convey no authority."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from backend.app.mcp_server.contracts import DiagnosticAccess


def actor(**changes):
    return replace(DiagnosticAccess(1, 2, frozenset({"printers:read"}), False, None, False, 1), **changes)


def test_domain_permission_matrix_has_all_nine_tools():
    from backend.app.services.farm_diagnostics.access import REQUIREMENTS, available

    assert len(REQUIREMENTS) == 9
    assert available(actor(), "bamdude_get_farm_overview")
    assert available(actor(), "bamdude_inspect_printer")
    assert not available(actor(), "bamdude_get_system_health")
    admin = actor(is_admin=True)
    assert available(admin, "bamdude_get_system_health")
    assert not available(replace(admin, printer_ids=frozenset()), "bamdude_get_system_health")
    assert not available(admin, "bamdude_read_logs")
    assert available(replace(admin, logs_allowed=True), "bamdude_read_logs")
    assert not available(
        actor(permissions=frozenset({"printers:read", "archives:read_own"})), "bamdude_get_print_timeline"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"key_id": 3},
        {"owner_id": 3},
        {"printer_ids": frozenset()},
        {"printer_ids": frozenset({1})},
        {"epoch": 2},
        {"is_admin": True},
        {"permissions": frozenset({"printers:read", "queue:read_all"})},
    ],
)
def test_cursor_is_bound_to_actor_scope_permissions_and_epoch(changes):
    from backend.app.services.farm_diagnostics.access import DiagnosticError
    from backend.app.services.farm_diagnostics.paging import decode, encode

    token = encode(actor(), "overview", {"limit": 2}, last=2, upper=5)
    assert decode(token, actor(), "overview", {"limit": 2}) == (2, 5)
    with pytest.raises(DiagnosticError, match="invalid_cursor"):
        decode(token, actor(**changes), "overview", {"limit": 2})


def test_cursor_rejects_tamper_other_filter_tool_expiry_and_restart(monkeypatch):
    from backend.app.services.farm_diagnostics import paging
    from backend.app.services.farm_diagnostics.access import DiagnosticError

    token = paging.encode(actor(), "overview", {}, last=1, upper=3)
    for value, tool, filters in [(token + "x", "overview", {}), (token, "other", {}), (token, "overview", {"x": 1})]:
        with pytest.raises(DiagnosticError, match="invalid_cursor"):
            paging.decode(value, actor(), tool, filters)
    with monkeypatch.context() as patch:
        patch.setattr(paging.time, "time", lambda: datetime.now(timezone.utc).timestamp() + 601)
        with pytest.raises(DiagnosticError, match="invalid_cursor"):
            paging.decode(token, actor(), "overview", {})
    monkeypatch.setattr(paging, "_SECRET", b"new-process")
    with pytest.raises(DiagnosticError, match="invalid_cursor"):
        paging.decode(token, actor(), "overview", {})


def test_inputs_reject_unknown_fields_bad_ids_ranges_and_timezones():
    from backend.app.services.farm_diagnostics.contracts import InspectInput, PageInput, PeriodInput

    for data in ({"limit": 101}, {"limit": True}, {"path": "/tmp/anything"}, {"cursor": "x" * 2049}):
        with pytest.raises(ValidationError):
            PageInput.model_validate(data)
    for value in (0, -1, True, "1"):
        with pytest.raises(ValidationError):
            InspectInput(printer_id=value)
    now = datetime.now(timezone.utc)
    valid = {"from": now - timedelta(days=1), "to": now, "timezone": "Europe/Kyiv"}
    assert PeriodInput.model_validate(valid).timezone == "Europe/Kyiv"
    for change in (
        {"timezone": "wrong/zone"},
        {"to": now + timedelta(hours=1)},
        {"from": now},
        {"from": now.replace(tzinfo=None)},
        {"from": now - timedelta(days=32)},
    ):
        with pytest.raises(ValidationError):
            PeriodInput.model_validate({**valid, **change})
