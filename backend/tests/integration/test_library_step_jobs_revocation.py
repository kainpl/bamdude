"""Revocation barriers for every actor kind (spec §5, plan N7): a right that goes between a job's start and its
attach leaves that receipt without success or metadata — read by the real checks on a fresh session — while
another actor's valid receipt on the same execution still gets the picture."""

from __future__ import annotations

import asyncio

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
from backend.app.services import library_preview_jobs as jobs_module
from backend.app.services.library_preview_jobs import Authority, StepSource
from backend.tests.auth_helpers import bind_test_key, mint_test_authority

pytestmark = pytest.mark.integration
UPLOAD, UPDATE_ALL = Permission.LIBRARY_UPLOAD.value, Permission.LIBRARY_UPDATE_ALL.value


class Gate:
    """Admitted and running; the attach waits for ``release`` and asks the authority again first."""

    def __init__(self):
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def __call__(self, source, *, order, wait_seconds, on_admit, on_hand_off, before_attach):
        on_admit()
        self.entered.set()
        await asyncio.wait_for(self.release.wait(), timeout=30)
        on_hand_off()
        return "succeeded" if await before_attach() else "authority_missing"


@pytest.fixture
async def jobs():
    gate = Gate()
    service = jobs_module.start_library_preview_jobs(perform=gate)  # the real DatabaseChecks
    service.gate = gate
    try:
        yield service
    finally:
        gate.release.set()


async def _group(db, name, permissions):
    group = Group(name=name, description="test", permissions=permissions)
    db.add(group)
    await db.flush()
    return group


async def _user(db, name, permissions):
    group = await _group(db, f"g-{name}", permissions)
    user = User(username=name, email=f"{name}@example.com", password_hash="x", role="user", is_active=True)
    user.groups.append(group)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    user._test_capture = await mint_test_authority(db, username=name)
    return user


async def _source(db):
    row = LibraryFile(filename="p.step", file_path="library/p.step", file_type="step", file_size=1, file_hash="a" * 64)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return StepSource.capture(row)


async def _settled(*receipts):
    """Each receipt AND its execution at a terminal: a receipt can end at the recheck before its execution does."""
    for _ in range(400):
        if all(not r.live and r.execution.state in jobs_module.TERMINAL for r in receipts):
            return
        await asyncio.sleep(0.01)
    raise AssertionError([(r.state, r.error_code) for r in receipts])


@pytest.mark.asyncio
async def test_a_user_whose_group_loses_the_right_mid_job_gets_nothing_another_actor_still_does(db_session, jobs):
    loser = await _user(db_session, "loser", [UPDATE_ALL])
    keeper = await _user(db_session, "keeper", [UPDATE_ALL])
    source = await _source(db_session)
    lost = jobs.enqueue(source, Authority.user(loser.id, capture=loser._test_capture), "manual")
    kept = jobs.enqueue(source, Authority.user(keeper.id, capture=keeper._test_capture), "manual")
    await asyncio.wait_for(jobs.gate.entered.wait(), timeout=10)
    group = (await db_session.get(User, loser.id)).groups[0]
    group.permissions = [UPLOAD]
    await db_session.commit()
    jobs.gate.release.set()
    await _settled(lost, kept)
    assert (lost.state, lost.error_code) == ("failed", "authority_missing")
    assert kept.state == "succeeded"


@pytest.mark.asyncio
async def test_an_api_key_disabled_mid_job_gets_nothing(db_session, jobs):
    owner = await _user(db_session, "keyowner", [UPLOAD, UPDATE_ALL])
    _, key_hash, key_prefix = generate_api_key()
    key = APIKey(
        name="k", key_hash=key_hash, key_prefix=key_prefix, enabled=True, user_id=owner.id, can_manage_library=True
    )
    await bind_test_key(db_session, key, owner)
    db_session.add(key)
    await db_session.commit()
    await db_session.refresh(key)
    receipt = jobs.enqueue(await _source(db_session), Authority.api_key(key.id, capture=capture_key(key)), "manual")
    await asyncio.wait_for(jobs.gate.entered.wait(), timeout=10)
    key.enabled = False
    await db_session.commit()
    jobs.gate.release.set()
    await _settled(receipt)
    assert (receipt.state, receipt.error_code) == ("failed", "authority_missing")
    assert receipt.execution.state == "failed"  # no receipt left with its right: nothing is attached


@pytest.mark.asyncio
async def test_a_telegram_provider_disabled_mid_job_gets_nothing(db_session, jobs):
    provider = NotificationProvider(name="bot", provider_type="telegram", enabled=True, config='{"bot_token": "1:x"}')
    db_session.add(provider)
    await db_session.flush()
    group = await _group(db_session, "g-chat", [UPLOAD])
    chat = TelegramChat(chat_id=55, provider_id=provider.id, is_active=True, group_id=group.id)
    db_session.add(chat)
    await db_session.commit()
    await db_session.refresh(chat)
    receipt = jobs.enqueue(await _source(db_session), Authority.telegram_chat(chat.id, provider.id, 55), "auto")
    await asyncio.wait_for(jobs.gate.entered.wait(), timeout=10)
    provider.enabled = False
    await db_session.commit()
    jobs.gate.release.set()
    await _settled(receipt)
    assert (receipt.state, receipt.error_code) == ("failed", "authority_missing")
    assert receipt.execution.state == "failed"  # no receipt left with its right: nothing is attached


@pytest.mark.asyncio
async def test_a_telegram_chat_moved_to_a_group_without_the_right_before_its_start_runs_nothing(db_session, jobs):
    blocker_user = await _user(db_session, "blocker", [UPDATE_ALL])
    provider = NotificationProvider(name="bot2", provider_type="telegram", enabled=True, config='{"bot_token": "2:x"}')
    db_session.add(provider)
    await db_session.flush()
    allowed = await _group(db_session, "g-allowed", [UPLOAD])
    denied = await _group(db_session, "g-denied", [Permission.PRINTERS_READ.value])
    chat = TelegramChat(chat_id=56, provider_id=provider.id, is_active=True, group_id=allowed.id)
    db_session.add(chat)
    await db_session.commit()
    await db_session.refresh(chat)
    blocker = jobs.enqueue(
        await _source(db_session), Authority.user(blocker_user.id, capture=blocker_user._test_capture), "manual"
    )
    await asyncio.wait_for(jobs.gate.entered.wait(), timeout=10)  # the blocker holds the runner
    other = LibraryFile(
        filename="q.step", file_path="library/q.step", file_type="step", file_size=1, file_hash="b" * 64
    )
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)
    waiting = jobs.enqueue(StepSource.capture(other), Authority.telegram_chat(chat.id, provider.id, 56), "auto")
    row = await db_session.get(TelegramChat, chat.id)
    row.group_id = denied.id
    await db_session.commit()
    jobs.gate.release.set()
    await _settled(blocker, waiting)
    assert (waiting.state, waiting.error_code) == ("failed", "authority_missing")
    assert waiting.execution.attempts == 0  # refused before any CAD
