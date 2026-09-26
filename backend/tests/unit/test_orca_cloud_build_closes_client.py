"""A failed authenticated Orca Cloud build closes its HTTP client (upstream f3b1c591).

``OrcaCloudService`` owns an httpx client from construction, and every step of
``_build_authenticated_service`` after that can raise: no stored refresh token,
a rejected refresh, an unreachable Orca, the token-rotation write. On success
the caller closes the client; on failure nobody was ever handed it, so each
failure leaked one into the pool. Services build one per sync, push and
resolve — a stuck sign-in failed on every one of them.

The unwind catches BaseException (a cancelled request leaks the client just the
same), and a failing close never replaces the error the caller needs to see.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from backend.app.api.routes import orca_cloud as orca_routes
from backend.app.services.orca_cloud import OrcaCloudAuthError, OrcaCloudError


class _Service:
    instances: list[_Service] = []

    def __init__(self, *, authenticated=False, refresh_error=None, close_error=None):
        self._authenticated = authenticated
        self._refresh_error = refresh_error
        self._close_error = close_error
        self.refresh_token = None
        self.access_token = None
        self.token_expiry = None
        self.granted_scope = None
        self.closed = 0
        _Service.instances.append(self)

    def set_tokens(self, token, refresh_token, expires_at):
        self.access_token, self.refresh_token, self.token_expiry = token, refresh_token, expires_at

    @property
    def is_authenticated(self):
        return self._authenticated

    async def refresh(self):
        if self._refresh_error is not None:
            raise self._refresh_error

    async def close(self):
        self.closed += 1
        if self._close_error is not None:
            raise self._close_error


def _creds(refresh_token="r1"):
    return SimpleNamespace(token="t1", refresh_token=refresh_token, expires_at=None, scope=None)


async def _build(service: _Service, creds, *, persist_error=None):
    persist = AsyncMock(side_effect=persist_error)
    with (
        patch.object(orca_routes, "OrcaCloudService", lambda: service),
        patch.object(orca_routes, "_load_credentials", AsyncMock(return_value=creds)),
        patch.object(orca_routes, "_clear_credentials", AsyncMock()),
        patch.object(orca_routes, "_persist_rotated_tokens", persist),
    ):
        return await orca_routes._build_authenticated_service(None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service", "creds", "persist_error"),
    [
        (_Service(), _creds(refresh_token=None), None),  # nothing to refresh with
        (_Service(refresh_error=OrcaCloudAuthError("revoked")), _creds(), None),
        (_Service(refresh_error=OrcaCloudError("unreachable")), _creds(), None),
        (_Service(), _creds(), RuntimeError("database is locked")),  # the rotation write
        (_Service(refresh_error=asyncio.CancelledError()), _creds(), None),
    ],
)
async def test_every_failure_closes_the_client(service, creds, persist_error):
    with pytest.raises((HTTPException, RuntimeError, asyncio.CancelledError)):
        await _build(service, creds, persist_error=persist_error)
    assert service.closed == 1


@pytest.mark.asyncio
async def test_a_failing_close_does_not_mask_the_real_error():
    service = _Service(refresh_error=OrcaCloudError("unreachable"), close_error=OSError("close failed"))
    with pytest.raises(HTTPException) as refused:
        await _build(service, _creds())
    assert refused.value.status_code == 502


@pytest.mark.asyncio
async def test_a_successful_build_leaves_the_client_to_its_caller():
    service = _Service(authenticated=True)
    assert await _build(service, _creds()) is service
    assert service.closed == 0
