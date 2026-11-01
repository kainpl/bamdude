"""The diagnostic SQL predicate distinguishes null, empty and disjoint scopes."""

from dataclasses import replace

import pytest
from sqlalchemy import func, select

from backend.app.mcp_server.contracts import DiagnosticAccess
from backend.app.models.printer import Printer
from backend.app.services.farm_diagnostics.access import REQUIREMENTS, printer_scope


@pytest.mark.parametrize("scope", ["null", "empty", "a", "b"])
async def test_printer_scope_precedes_selection_and_aggregation(printer_factory, db_session, scope):
    a, b = await printer_factory(name="A"), await printer_factory(name="B")
    ids = {"null": None, "empty": frozenset(), "a": frozenset({a.id}), "b": frozenset({b.id})}[scope]
    access = DiagnosticAccess(1, 1, frozenset({"printers:read"}), False, ids, False, 1)
    predicate = printer_scope(access, Printer.id)
    result = list((await db_session.scalars(select(Printer.id).where(predicate).order_by(Printer.id).limit(1))).all())
    count = await db_session.scalar(select(func.count(Printer.id)).where(predicate))
    expected = [a.id, b.id] if ids is None else sorted(ids)
    assert result == expected[:1] and count == len(expected)
    assert replace(access, printer_ids=frozenset()).printer_ids is not None


def test_diagnostic_permissions_stay_within_existing_read_key_flags():
    from backend.app.api.routes.print_state_recovery import READ_PERMISSIONS
    from backend.app.core.auth import _resolve_apikey_scope

    assert REQUIREMENTS["bamdude_list_state_issues"] == frozenset(p.value for p in READ_PERMISSIONS)
    assert REQUIREMENTS["bamdude_explain_recovery"] == frozenset(p.value for p in READ_PERMISSIONS)
    for permissions in REQUIREMENTS.values():
        assert permissions
        for permission in permissions:
            assert _resolve_apikey_scope(permission) == "can_read_status"
