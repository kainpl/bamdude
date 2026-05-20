"""Regression test for upstream Bambuddy #1292: LDAP login must not wipe
manually-assigned non-LDAP-managed groups.

Pin the partitioning behaviour of ``_sync_ldap_user``:

- Groups whose names are in ``ldap_config.group_mapping.values()`` ∪
  ``{ldap_config.default_group}`` are "LDAP-managed" and get rebuilt
  from LDAP truth on every login (so revocation in LDAP propagates).
- Every other group attached to the user is treated as a manual admin
  assignment and is preserved across logins.

Before this fix, ``_sync_ldap_user`` blew away ``user.groups`` entirely
on each login — assigning an LDAP user to "Administrators" while
``ldap_group_mapping`` only covered "Users" was reverted on the user's
next login.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_group(group_id: int, name: str):
    g = MagicMock()
    g.id = group_id
    g.name = name
    return g


def _make_ldap_user(username: str, email: str, groups: list[str]):
    return SimpleNamespace(username=username, email=email, groups=groups)


def _make_ldap_config(*, group_mapping: dict[str, str] | None = None, default_group: str | None = None):
    return SimpleNamespace(group_mapping=group_mapping or {}, default_group=default_group)


def _make_db(group_lookup: dict[str, MagicMock]):
    """Build an AsyncMock db whose ``execute(select(Group).where(name.in_(...)))``
    yields the groups requested by the route."""
    db = AsyncMock()

    captured = {"names": None}

    async def execute(stmt):
        # Sniff the IN-clause to know which group names were requested. We
        # don't fully reconstruct the SQL — just look at the compiled string.
        captured["names"] = [
            name for name in group_lookup if f"'{name}'" in str(stmt.compile(compile_kwargs={"literal_binds": True}))
        ]
        result = MagicMock()
        result.scalars = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=[group_lookup[n] for n in captured["names"]]))
        )
        return result

    db.execute = execute
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_manual_assignment_to_non_ldap_group_survives_login():
    """The reporter's exact scenario: admin assigns 'Administrators' to an
    LDAP-authenticated user; ``ldap_group_mapping`` only covers 'Users'.
    Login must not wipe the Administrators assignment."""
    from backend.app.api.routes.auth import _sync_ldap_user

    users_group = _make_group(1, "Users")
    admin_group = _make_group(2, "Administrators")  # manual, NOT LDAP-mapped

    user = MagicMock()
    user.username = "alice"
    user.email = "alice@example.com"
    user.groups = [users_group, admin_group]

    ldap_user = _make_ldap_user("alice", "alice@example.com", ["cn=users-ldap,dc=example,dc=com"])
    ldap_config = _make_ldap_config(group_mapping={"cn=users-ldap,dc=example,dc=com": "Users"})

    db = _make_db({"Users": users_group})

    await _sync_ldap_user(db, user, ldap_user, ldap_config)

    final_names = sorted(g.name for g in user.groups)
    assert "Administrators" in final_names, "manual non-LDAP group must survive login"
    assert "Users" in final_names, "LDAP-managed group must still be applied"


@pytest.mark.asyncio
async def test_ldap_revocation_propagates_for_managed_group():
    """If the user was previously in an LDAP-managed group but LDAP no
    longer maps them there, the managed group must be removed."""
    from backend.app.api.routes.auth import _sync_ldap_user

    users_group = _make_group(1, "Users")
    operators_group = _make_group(2, "Operators")

    user = MagicMock()
    user.username = "alice"
    user.email = "alice@example.com"
    # User WAS in both LDAP-managed groups; LDAP now only maps Users
    user.groups = [users_group, operators_group]

    ldap_user = _make_ldap_user("alice", "alice@example.com", ["cn=users-ldap,dc=example,dc=com"])
    ldap_config = _make_ldap_config(
        group_mapping={
            "cn=users-ldap,dc=example,dc=com": "Users",
            "cn=operators-ldap,dc=example,dc=com": "Operators",
        }
    )

    db = _make_db({"Users": users_group, "Operators": operators_group})

    await _sync_ldap_user(db, user, ldap_user, ldap_config)

    final_names = sorted(g.name for g in user.groups)
    assert "Users" in final_names
    assert "Operators" not in final_names, "revoked LDAP-managed group must be removed"


@pytest.mark.asyncio
async def test_manual_assignment_to_ldap_managed_group_is_overridden():
    """Edge case explicitly covered: a manual assignment to a group that
    IS in the LDAP mapping is still overridden by LDAP state — once an
    assignment is in user_groups you can't tell manual-but-mapped from
    LDAP-derived, so LDAP wins for any group it has authority over."""
    from backend.app.api.routes.auth import _sync_ldap_user

    users_group = _make_group(1, "Users")

    user = MagicMock()
    user.username = "alice"
    user.email = "alice@example.com"
    # Manual admin assignment to "Users" even though LDAP doesn't grant it
    user.groups = [users_group]

    ldap_user = _make_ldap_user("alice", "alice@example.com", [])  # LDAP says NO groups
    ldap_config = _make_ldap_config(group_mapping={"cn=users-ldap,dc=example,dc=com": "Users"})

    db = _make_db({"Users": users_group})

    await _sync_ldap_user(db, user, ldap_user, ldap_config)

    final_names = [g.name for g in user.groups]
    assert "Users" not in final_names, "Users is LDAP-managed; manual assignment loses to empty-LDAP-state on login"


@pytest.mark.asyncio
async def test_default_group_applied_when_ldap_has_no_mapping():
    """When the user has no LDAP-mapped groups, fall back to default_group.
    default_group is itself LDAP-managed (so it can be replaced later)."""
    from backend.app.api.routes.auth import _sync_ldap_user

    viewers_group = _make_group(3, "Viewers")
    admin_group = _make_group(2, "Administrators")  # manual, must survive

    user = MagicMock()
    user.username = "alice"
    user.email = "alice@example.com"
    user.groups = [admin_group]

    ldap_user = _make_ldap_user("alice", "alice@example.com", [])
    ldap_config = _make_ldap_config(
        group_mapping={"cn=users-ldap,dc=example,dc=com": "Users"},
        default_group="Viewers",
    )

    db = _make_db({"Viewers": viewers_group})

    await _sync_ldap_user(db, user, ldap_user, ldap_config)

    final_names = sorted(g.name for g in user.groups)
    assert "Administrators" in final_names, "manual group survives"
    assert "Viewers" in final_names, "default group applied"
