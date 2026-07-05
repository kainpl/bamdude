"""API-key permission allowlist — pure-logic guards (GHSA-r2qv-8222-hqg3).

Locks the fail-closed allowlist that gates ``require_permission`` /
``require_any_permission`` / ``require_ownership_permission`` for API keys.
Before this, any valid key satisfied every non-webhook route regardless of its
scope flags — a "queue-only" key could stop prints, delete other users'
library files, and read every endpoint. See ``core/auth.py``.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.app.core.auth import (
    _APIKEY_DENIED_PERMISSIONS,
    _APIKEY_SCOPE_BY_PERMISSION,
    _check_apikey_permissions,
    _resolve_apikey_scope,
)
from backend.app.core.permissions import Permission

# The scope flags an APIKey row can carry (mirrors backend/app/models/api_key.py).
_ALL_SCOPE_FLAGS = {
    "can_read_status",
    "can_queue",
    "can_control_printer",
    "can_manage_library",
    "can_manage_inventory",
    "can_access_cloud",
}


def _key(**flags: bool) -> SimpleNamespace:
    """A stand-in APIKey with every scope flag defaulting to False."""
    base = dict.fromkeys(_ALL_SCOPE_FLAGS, False)
    base.update(flags)
    return SimpleNamespace(**base)


def test_every_permission_has_a_classification():
    """Structural drift backstop: a new Permission added without an allowlist
    or denylist entry must fail CI (the old denylist shape let new perms
    silently widen the API-key surface)."""
    classified = set(_APIKEY_SCOPE_BY_PERMISSION) | _APIKEY_DENIED_PERMISSIONS
    unclassified = [p for p in Permission if p not in classified]
    assert not unclassified, f"Permissions missing allowlist/denylist entry: {unclassified}"


def test_allowlist_and_denylist_are_disjoint():
    overlap = set(_APIKEY_SCOPE_BY_PERMISSION) & _APIKEY_DENIED_PERMISSIONS
    assert not overlap, f"Permissions in both allowlist and denylist: {overlap}"


def test_allowlist_scope_flags_are_real_columns():
    """Every mapped scope attr must exist as a real flag name."""
    assert set(_APIKEY_SCOPE_BY_PERMISSION.values()) <= _ALL_SCOPE_FLAGS


def test_resolve_scope_unknown_and_admin_return_none():
    assert _resolve_apikey_scope("not:a:real:permission") is None
    # SETTINGS_UPDATE is admin-only (denylist) → unmapped → None
    assert _resolve_apikey_scope(Permission.SETTINGS_UPDATE.value) is None
    assert _resolve_apikey_scope(Permission.PRINTERS_READ.value) == "can_read_status"


def test_read_status_gates_reads():
    reader = _key(can_read_status=True)
    _check_apikey_permissions(reader, [Permission.PRINTERS_READ.value])  # no raise
    with pytest.raises(HTTPException) as ei:
        _check_apikey_permissions(_key(), [Permission.PRINTERS_READ.value])
    assert ei.value.status_code == 403


def test_queue_scope_does_not_leak_into_library_or_control():
    """A "queue-only" key cannot upload library files or control the printer."""
    queue_key = _key(can_queue=True)
    _check_apikey_permissions(queue_key, [Permission.QUEUE_CREATE.value])  # ok
    for denied in (Permission.LIBRARY_UPLOAD, Permission.PRINTERS_CONTROL, Permission.INVENTORY_UPDATE):
        with pytest.raises(HTTPException):
            _check_apikey_permissions(queue_key, [denied.value])


def test_library_and_inventory_scopes_are_independent():
    lib = _key(can_manage_library=True)
    inv = _key(can_manage_inventory=True)
    _check_apikey_permissions(lib, [Permission.LIBRARY_UPLOAD.value])
    _check_apikey_permissions(inv, [Permission.INVENTORY_UPDATE.value])
    with pytest.raises(HTTPException):
        _check_apikey_permissions(lib, [Permission.INVENTORY_UPDATE.value])
    with pytest.raises(HTTPException):
        _check_apikey_permissions(inv, [Permission.LIBRARY_UPLOAD.value])


def test_manage_library_key_can_curate_all_but_not_purge():
    """#1832: a Manage-Library API key curates any user's library files —
    LIBRARY_UPDATE_ALL / DELETE_ALL ride can_manage_library (API keys have no
    per-row ownership identity, so require_ownership_permission gates them on the
    ALL perm). LIBRARY_PURGE stays admin-only (bypasses the soft-delete window)."""
    lib = _key(can_manage_library=True)
    _check_apikey_permissions(lib, [Permission.LIBRARY_UPDATE_ALL.value])
    _check_apikey_permissions(lib, [Permission.LIBRARY_DELETE_ALL.value])
    with pytest.raises(HTTPException):
        _check_apikey_permissions(lib, [Permission.LIBRARY_PURGE.value])
    # A key without the library scope still can't curate.
    with pytest.raises(HTTPException):
        _check_apikey_permissions(_key(can_queue=True), [Permission.LIBRARY_UPDATE_ALL.value])


def test_admin_permission_denied_even_with_all_flags():
    full = _key(**dict.fromkeys(_ALL_SCOPE_FLAGS, True))
    for admin in (
        Permission.SETTINGS_UPDATE,
        Permission.USERS_CREATE,
        Permission.PRINTERS_CREATE,
        Permission.DISCOVERY_SCAN,
    ):
        with pytest.raises(HTTPException) as ei:
            _check_apikey_permissions(full, [admin.value])
        assert "administrative" in ei.value.detail


def test_require_all_needs_every_permission():
    key = _key(can_read_status=True)  # has read, not queue
    with pytest.raises(HTTPException):
        _check_apikey_permissions(key, [Permission.PRINTERS_READ.value, Permission.QUEUE_CREATE.value])


def test_require_any_needs_only_one():
    key = _key(can_read_status=True)
    # passes because PRINTERS_READ is granted even though QUEUE_CREATE is not
    _check_apikey_permissions(key, [Permission.QUEUE_CREATE.value, Permission.PRINTERS_READ.value], require_any=True)
    # fails when none are granted
    with pytest.raises(HTTPException):
        _check_apikey_permissions(
            key, [Permission.QUEUE_CREATE.value, Permission.LIBRARY_UPLOAD.value], require_any=True
        )


def test_empty_permission_list_fails_closed():
    with pytest.raises(HTTPException) as ei:
        _check_apikey_permissions(_key(can_read_status=True), [])
    assert ei.value.status_code == 403


def test_reprint_rides_can_queue_not_archive_admin():
    """ARCHIVES_REPRINT_* is a queue op; ARCHIVES_CREATE/DELETE stay admin."""
    key = _key(can_queue=True)
    _check_apikey_permissions(key, [Permission.ARCHIVES_REPRINT_OWN.value])
    with pytest.raises(HTTPException):
        _check_apikey_permissions(key, [Permission.ARCHIVES_DELETE_OWN.value])
