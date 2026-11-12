"""A STEP job's authority, read again (spec §5, plan A4/N7): a user's active flag and group rights, an API key's
validity, scope and owner, a Telegram chat's row, provider and group — each read afresh on its own short session
with nothing written (no ``last_used``), and a missing descriptor is never the system."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.core.auth import generate_api_key
from backend.app.core.auth_lifetime import capture_key
from backend.app.core.permissions import Permission
from backend.app.models.api_key import APIKey
from backend.app.models.group import Group
from backend.app.models.library import LibraryFile
from backend.app.models.notification import NotificationProvider
from backend.app.models.telegram_chat import TelegramChat
from backend.app.models.user import User
from backend.app.services.library_preview_jobs import Authority, DatabaseChecks, StepSource
from backend.tests.auth_helpers import bind_test_key, mint_test_authority

pytestmark = pytest.mark.integration

UPLOAD, UPDATE_ALL = Permission.LIBRARY_UPLOAD.value, Permission.LIBRARY_UPDATE_ALL.value


async def _group(db, name, permissions):
    group = Group(name=name, description="test", permissions=permissions)
    db.add(group)
    await db.flush()
    return group


async def _user(db, name, permissions, *, active=True):
    group = await _group(db, f"g-{name}", permissions)
    user = User(username=name, email=f"{name}@example.com", password_hash="x", role="user", is_active=True)
    user.groups.append(group)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    user._test_capture = await mint_test_authority(db, username=name)
    if not active:
        user.is_active = False
        await db.commit()
    return user


async def _key(db, owner, **flags):
    _, key_hash, key_prefix = generate_api_key()
    key = APIKey(name=f"k-{key_prefix}", key_hash=key_hash, key_prefix=key_prefix, enabled=True, user_id=owner.id)
    for name, value in flags.items():
        setattr(key, name, value)
    await bind_test_key(db, key, owner)
    db.add(key)
    await db.commit()
    await db.refresh(key)
    return key


async def _chat(db, permissions, *, active=True, provider_enabled=True):
    provider = NotificationProvider(
        name="bot", provider_type="telegram", enabled=provider_enabled, config='{"bot_token": "1:x"}'
    )
    db.add(provider)
    await db.flush()
    group = await _group(db, f"g-chat-{provider.id}", permissions)
    chat = TelegramChat(chat_id=4242, provider_id=provider.id, is_active=active, group_id=group.id)
    db.add(chat)
    await db.commit()
    await db.refresh(chat)
    return chat


@pytest.mark.asyncio
async def test_a_user_holds_a_purpose_only_with_its_permission_and_while_active(db_session):
    from sqlalchemy import delete

    uploader = await _user(db_session, "uploader", [UPLOAD])
    admin = await _user(db_session, "admin2", [UPLOAD, UPDATE_ALL])
    gone = await _user(db_session, "gone", [UPLOAD, UPDATE_ALL], active=False)
    deleted = await _user(db_session, "deleted", [UPLOAD, UPDATE_ALL])
    deleted_actor = Authority.user(deleted.id, capture=deleted._test_capture)
    await db_session.execute(delete(User).where(User.id == deleted.id))
    await db_session.commit()
    checks = DatabaseChecks()
    assert await checks.authority_holds(Authority.user(uploader.id, capture=uploader._test_capture), "auto")
    assert not await checks.authority_holds(
        Authority.user(uploader.id, capture=uploader._test_capture), "manual"
    )  # the button is update_all
    assert await checks.authority_holds(Authority.user(admin.id, capture=admin._test_capture), "manual")
    assert not await checks.authority_holds(Authority.user(gone.id, capture=gone._test_capture), "auto")
    assert not await checks.authority_holds(Authority.user(999_999), "auto")
    assert not await checks.authority_holds(deleted_actor, "auto")  # a real issued capture, now without its account


@pytest.mark.asyncio
async def test_a_right_removed_from_the_group_is_seen_at_once(db_session):
    user = await _user(db_session, "demoted", [UPLOAD, UPDATE_ALL])
    checks = DatabaseChecks()
    assert await checks.authority_holds(Authority.user(user.id, capture=user._test_capture), "manual")
    group = (await db_session.get(User, user.id)).groups[0]
    group.permissions = [UPLOAD]
    await db_session.commit()
    assert not await checks.authority_holds(Authority.user(user.id, capture=user._test_capture), "manual")  # no cache


@pytest.mark.asyncio
async def test_an_api_key_holds_while_enabled_unexpired_scoped_and_owned_and_last_used_stays(db_session):
    owner = await _user(db_session, "keyowner", [UPLOAD, UPDATE_ALL])
    key = await _key(db_session, owner, can_manage_library=True)
    checks = DatabaseChecks()
    assert await checks.authority_holds(Authority.api_key(key.id, capture=capture_key(key)), "manual")
    await db_session.refresh(key)
    assert key.last_used is None  # read only: nothing written by the check

    unscoped = await _key(db_session, owner, can_manage_library=False)
    assert not await checks.authority_holds(Authority.api_key(unscoped.id, capture=capture_key(unscoped)), "manual")

    expired = await _key(db_session, owner, can_manage_library=True)
    expired.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.commit()
    assert not await checks.authority_holds(Authority.api_key(expired.id, capture=capture_key(expired)), "manual")

    key.enabled = False
    await db_session.commit()
    assert not await checks.authority_holds(Authority.api_key(key.id, capture=capture_key(key)), "manual")


@pytest.mark.asyncio
async def test_an_api_key_dies_with_its_owner(db_session):
    owner = await _user(db_session, "leaver", [UPLOAD, UPDATE_ALL])
    key = await _key(db_session, owner, can_manage_library=True)
    checks = DatabaseChecks()
    assert await checks.authority_holds(Authority.api_key(key.id, capture=capture_key(key)), "manual")
    owner_row = await db_session.get(User, owner.id)
    owner_row.is_active = False
    await db_session.commit()
    assert not await checks.authority_holds(Authority.api_key(key.id, capture=capture_key(key)), "manual")


@pytest.mark.asyncio
async def test_a_telegram_chat_holds_only_for_its_own_upload_while_chat_provider_and_group_hold(db_session):
    chat = await _chat(db_session, [UPLOAD, UPDATE_ALL])
    checks = DatabaseChecks()
    authority = Authority.telegram_chat(chat.id, chat.provider_id, chat.chat_id)
    assert await checks.authority_holds(authority, "auto")
    assert not await checks.authority_holds(authority, "manual")  # a chat never presses the button, whatever its group
    assert not await checks.authority_holds(
        Authority.telegram_chat(chat.id, chat.provider_id + 1, chat.chat_id), "auto"
    )  # the pair must match the row
    provider = await db_session.get(NotificationProvider, chat.provider_id)
    provider.enabled = False
    await db_session.commit()
    assert not await checks.authority_holds(authority, "auto")
    provider.enabled = True
    row = await db_session.get(TelegramChat, chat.id)
    row.is_active = False
    await db_session.commit()
    assert not await checks.authority_holds(authority, "auto")


@pytest.mark.asyncio
async def test_a_telegram_group_without_library_upload_holds_nothing(db_session):
    chat = await _chat(db_session, [Permission.PRINTERS_READ.value])
    authority = Authority.telegram_chat(chat.id, chat.provider_id, chat.chat_id)
    assert not await DatabaseChecks().authority_holds(authority, "auto")


@pytest.mark.asyncio
async def test_the_source_holds_only_while_the_row_is_unchanged_and_not_trashed(db_session):
    row = LibraryFile(
        filename="part.step",
        file_path="library/part.step",
        file_type="step",
        file_size=10,
        file_hash="a" * 64,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    checks = DatabaseChecks()
    captured = StepSource.capture(row)
    assert await checks.source_holds(captured)
    row.file_hash = "b" * 64
    await db_session.commit()
    assert not await checks.source_holds(captured)
    row.file_hash = "a" * 64
    row.deleted_at = datetime.now(timezone.utc)
    await db_session.commit()
    assert not await checks.source_holds(captured)
