"""Explicit current-credential fixtures; raw signers remain for crypto/legacy negatives.

Never change ORM defaults here: an ordinary APIKey/LL row is intentionally
legacy unless a fixture explicitly uses the current guarded issuance path.
"""

from sqlalchemy import select

from backend.app.core import database
from backend.app.core.auth_lifetime import VerifiedOutcome, capture_account, stage_session
from backend.app.models.user import User


async def mint_test_session(db, *, username: str | None = None, user: User | None = None, remember_me=False):
    if user is None:
        user = (await db.execute(select(User).where(User.username == username))).scalar_one()
    issued = await stage_session(
        db, VerifiedOutcome(capture_account(user), user.auth_source), remember_me=remember_me, issue_refresh=False
    )
    await db.commit()
    return issued


async def mint_test_access(db=None, *, username: str | None = None, user: User | None = None) -> str:
    """Persist a real AuthSession and return its signed access token.

    Supplying the fixture session avoids a second SQLite connection against
    pending setup writes. Otherwise use the normal test-patched factory.
    """
    if db is not None:
        return (await mint_test_session(db, username=username, user=user)).access_token
    async with database.async_session() as session:
        return (await mint_test_session(session, username=username, user=user)).access_token


async def bind_test_key(db, key, owner: User | None = None):
    """Bind only a NEW key fixture under the actual account guard, never revive legacy rows."""
    import uuid

    from backend.app.core.auth_lifetime import guard_accounts

    if key.id is not None or key.retired_at is not None or key.retired_reason is not None:
        raise ValueError("Only a fresh test key may be issued")
    if owner is None:
        target = User.id == key.user_id if key.user_id is not None else User.username == "test_admin"
        owner = (await db.execute(select(User).where(target))).scalar_one()
    await guard_accounts(db, [capture_account(owner, independent=True)])
    key.user_id = owner.id
    key.owner_identity = owner.account_identity
    key.contract_version = 1
    key.connector_identity = key.connector_identity or str(uuid.uuid4())
    return key


async def mint_test_authority(db=None, *, username="test_admin"):
    if db is not None:
        return (await mint_test_session(db, username=username)).capture
    async with database.async_session() as session:
        return (await mint_test_session(session, username=username)).capture


async def key_identity_for_test(key_id: int) -> str:
    from backend.app.models.api_key import APIKey

    async with database.async_session() as db:
        return (await db.execute(select(APIKey.connector_identity).where(APIKey.id == key_id))).scalar_one()
