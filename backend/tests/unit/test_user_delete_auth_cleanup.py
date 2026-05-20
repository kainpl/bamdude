"""Unit tests for ``delete_user`` orphan-auth cleanup.

Pinned behaviour (upstream Bambuddy #1285 / commit 4d8dbc83): deleting a
user must also delete every row in ``user_oidc_links``, ``user_totp``,
``user_otp_codes`` and ``long_lived_tokens`` referencing them. The
existing ``api_keys`` cleanup (#1182 pattern) is already covered.

These tests instantiate the route function directly with mocked DB calls
so they don't depend on the full integration harness — the assertion is
structural (each DELETE statement is issued), not behavioural.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.models.api_key import APIKey
from backend.app.models.long_lived_token import LongLivedToken
from backend.app.models.oidc_provider import UserOIDCLink
from backend.app.models.user_otp_code import UserOTPCode
from backend.app.models.user_totp import UserTOTP


def _collect_deleted_models(db_mock: AsyncMock) -> list[type]:
    """Return the list of ORM models passed into ``db.execute(delete(...))`` calls.

    Walks the ``call_args_list`` and pulls the ``entity_description`` out of
    each statement. Returns models in execution order.
    """
    models: list[type] = []
    for call in db_mock.execute.call_args_list:
        if not call.args:
            continue
        stmt = call.args[0]
        try:
            desc = stmt.entity_description
        except AttributeError:
            continue
        if desc:
            models.append(desc["entity"])
    return models


@pytest.mark.asyncio
async def test_delete_user_clears_oidc_mfa_otp_token_rows():
    """The four user-FK tables get an explicit DELETE issued by the route.

    Pinned because SQLite's ``foreign_keys=OFF`` means the model-level
    ``ON DELETE CASCADE`` never fires; without the explicit DELETEs, the
    rows persist as orphans and block SSO re-login (#1285).
    """
    # Build a mock user. Route reads `user_id` from the path parameter
    # but also fetches the user from the DB before deleting.
    user = MagicMock()
    user.id = 42
    user.username = "to-delete"
    user.is_active = True
    user.is_admin = False
    user.groups = []

    db = AsyncMock()
    # First db.execute → the SELECT for the user
    select_result = MagicMock()
    select_result.scalar_one_or_none = MagicMock(return_value=user)
    # The PrintQueueItem ID SELECT yields no rows for this test
    select_pq_result = MagicMock()
    select_pq_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    # Subsequent updates / deletes don't need a meaningful return
    db.execute = AsyncMock(side_effect=[select_result] + [select_pq_result] + [MagicMock()] * 50)
    db.delete = AsyncMock()
    db.commit = AsyncMock()

    from backend.app.api.routes.users import delete_user

    # Route's auth dependency is bypassed by passing a sentinel; same
    # pattern as how the route does in production. We pass a fake admin
    # token via ``_`` (the route uses RequirePermission which would
    # validate in the integration harness, not here at the call layer).
    admin = MagicMock()
    admin.id = 1
    admin.username = "admin"

    await delete_user(user_id=42, delete_items=False, current_user=admin, db=db)

    issued_deletes = _collect_deleted_models(db)
    # APIKey (existing) + UserOIDCLink + UserTOTP + UserOTPCode +
    # LongLivedToken (new) must ALL be issued.
    assert APIKey in issued_deletes, "APIKey cleanup must remain (regression guard)"
    assert UserOIDCLink in issued_deletes, "OIDC link cleanup missing (#1285)"
    assert UserTOTP in issued_deletes, "TOTP cleanup missing (#1285)"
    assert UserOTPCode in issued_deletes, "OTP-code cleanup missing (#1285)"
    assert LongLivedToken in issued_deletes, "long-lived token cleanup missing (#1285)"
    # Finally the user itself is deleted via db.delete(user)
    db.delete.assert_awaited_once_with(user)
    db.commit.assert_awaited()
