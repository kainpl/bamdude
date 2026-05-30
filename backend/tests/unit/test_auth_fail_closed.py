"""Fail-closed contract for the auth-state probes (GHSA-6mf4-q26m-47pv).

Upstream Bambuddy v0.2.4.4 fixed a CVSS 9.8 fail-open auth bypass: its
``is_auth_enabled`` and ``auth_middleware`` caught *every* exception during
the auth-state probe and returned the "allow" answer. An attacker who could
force a DB exception (the PoC floods ``/api/v1/auth/login`` to exhaust the
process file-descriptor budget) then hit any protected endpoint during the
fail-open window with no token at all.

**BamDude is architecturally immune to that exact bug**: auth is *always-on*,
so there is no per-request ``is_auth_enabled`` predicate that can short-circuit
the middleware into "auth disabled → allow everything" (see
``routes/auth.py:380`` keeping the legacy flag hard-``True`` and
``routes/mfa.py:594`` documenting the removal of the upstream gate). Our
``auth_middleware`` (``main.py``) fail-closes the auth gate to ``401`` and the
setup gate to ``503`` for every non-whitelisted route.

These tests pin the fail-closed contract of the auth-config predicates that
*do* exist in BamDude, so the GHSA anti-pattern (wrap a Settings/admin probe
in ``except Exception: return <allow>``) can never be reintroduced silently:

1. ``is_advanced_auth_enabled`` propagates any DB exception instead of
   swallowing it and returning ``False`` (the upstream advisory explicitly
   noted this predicate "already propagates correctly" — this locks it).
2. Its no-row / "true" / "false" happy paths are unchanged.
3. ``has_any_admin`` returns ``False`` on a DB error *by design* (a fresh
   install whose tables don't exist yet must be able to reach ``/auth/setup``)
   — and that is NOT an auth bypass because the setup gate still answers
   ``503`` for every non-whitelisted route. This test pins that intentional
   behaviour with the reasoning attached, so a future "harden it to propagate"
   change is a conscious decision, not an accident.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.api.routes.auth import is_advanced_auth_enabled
from backend.app.core.auth import has_any_admin


@pytest.mark.asyncio
async def test_is_advanced_auth_enabled_propagates_db_exception_instead_of_failing_open():
    """The GHSA-6mf4-q26m-47pv regression. A DB error during the advanced-auth
    probe must propagate — fail closed — instead of being swallowed and
    returning ``False`` (which would silently treat advanced auth as disabled
    on any DB hiccup)."""

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=OSError("simulated file-descriptor exhaustion"))

    with pytest.raises(OSError, match="simulated file-descriptor exhaustion"):
        await is_advanced_auth_enabled(db)


@pytest.mark.asyncio
async def test_is_advanced_auth_enabled_returns_false_when_settings_row_absent():
    """Legitimate 'never configured' path: the settings row simply does not
    exist. ``scalar_one_or_none`` returns ``None``, no exception, and the
    function returns ``False`` — by configuration, not because the DB blew up."""

    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=None)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    assert await is_advanced_auth_enabled(db) is False


@pytest.mark.asyncio
async def test_is_advanced_auth_enabled_returns_true_when_setting_value_is_true():
    """Happy path: the settings row exists and its value is ``"true"``."""

    setting = MagicMock()
    setting.value = "true"
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=setting)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    assert await is_advanced_auth_enabled(db) is True


@pytest.mark.asyncio
async def test_is_advanced_auth_enabled_returns_false_when_setting_value_is_false():
    """Happy path: the settings row exists and its value is ``"false"``."""

    setting = MagicMock()
    setting.value = "false"
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=setting)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    assert await is_advanced_auth_enabled(db) is False


@pytest.mark.asyncio
async def test_has_any_admin_returns_false_on_db_error_is_intentional_not_a_bypass():
    """``has_any_admin`` returns ``False`` on a DB error *on purpose* — a fresh
    install whose User/Group tables don't exist yet must be able to reach the
    ``/auth/setup`` whitelist route to create the first admin.

    This is NOT the GHSA fail-open: the setup gate in ``auth_middleware`` still
    answers ``503 setup_required`` for every non-whitelisted route when
    ``has_any_admin`` is ``False``, and ``/auth/setup`` itself re-guards with
    ``if await has_any_admin(db): raise "already completed"``. The auth gate
    (which actually grants access to protected resources) has no DB probe that
    can fail open. This test pins the intentional behaviour so any future change
    to propagate here is a conscious decision rather than a silent regression."""

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=OSError("simulated DB outage"))

    assert await has_any_admin(db) is False
