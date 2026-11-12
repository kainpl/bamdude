"""Independent DB connections exercise the real final-publication boundary."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.core.auth_lifetime import (
    VerifiedOutcome,
    capture_account,
    stage_end_sessions,
    stage_session,
)
from backend.app.core.auth_runtime import auth_runtime
from backend.app.core.config import settings
from backend.app.models.api_key import APIKey
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.queue_source import QueueSource
from backend.app.models.user import User
from backend.app.services import queue_ops, queue_sources
from backend.app.services.publication_authority import publication_checks
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401

pytestmark = pytest.mark.asyncio


async def principal(factory):
    auth_runtime.open_admission()
    async with factory() as db:
        user = User(username="publication-owner", role="admin", is_active=True)
        db.add(user)
        await db.flush()
        account = capture_account(user)
        issued = await stage_session(db, VerifiedOutcome(account, "local"), remember_me=False, issue_refresh=False)
        await db.commit()
        return account, issued.capture


async def test_revoke_after_capture_refuses_before_install_or_repair(test_engine, monkeypatch, tmp_path):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    account, capture = await principal(factory)
    queue_sources._reset_state()
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    source = tmp_path / "part.gcode"
    source.write_text("G28\nG1 X1 Y1\n", encoding="utf-8")
    receipt = await queue_sources.capture(queue_sources.CaptureRequest(source, "gcode", source.name))
    async with factory() as db:
        await stage_end_sessions(db, account)
        await db.commit()
    called = []

    async def install(*args):
        called.append("install")
        raise AssertionError("revoked principal reached filesystem publication")

    async def attach(*args):
        called.append("attach")

    monkeypatch.setattr(queue_sources, "_install", install)
    prepare, reread = publication_checks(capture)
    try:
        with pytest.raises(HTTPException) as refused:
            await queue_sources.publish(
                receipt, attach, session_factory=factory, before_publish=prepare, before_attach=reread
            )
        assert refused.value.status_code == 401
        assert called == []
        assert receipt.state == "staged"
        async with factory() as db:
            assert await db.scalar(select(func.count()).select_from(QueueSource)) == 0
    finally:
        await receipt.discard()
        queue_sources._reset_state()


async def test_final_writer_serializes_account_revocation(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    account, capture = await principal(factory)
    entered = asyncio.Event()

    async def revoke():
        async with factory() as db:
            entered.set()
            await stage_end_sessions(db, account)
            await db.commit()

    task = None
    try:
        async with factory() as db:
            prepare, reread = publication_checks(capture)
            await prepare(db)
            task = asyncio.create_task(revoke())
            await asyncio.wait_for(entered.wait(), 5)
            await asyncio.sleep(0.1)
            assert not task.done(), "revocation bypassed final writer's account guard"
            await reread(db)
            await db.commit()
        await asyncio.wait_for(task, 10)
        async with factory() as db:
            with pytest.raises(RuntimeError, match="guard is not held"):
                await reread(db)
            # The old transaction's proof cannot be reused in another writer.
            prepare_again, _ = publication_checks(capture)
            with pytest.raises(HTTPException) as refused:
                await prepare_again(db)
            assert refused.value.status_code == 401
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def clone_setup(factory):
    account, session = await principal(factory)
    async with factory() as db:
        printer = Printer(
            name="clone", serial_number="clone-proof", model="P1S", ip_address="127.0.0.1", access_code="x"
        )
        db.add(printer)
        await db.flush()
        db.add(PrinterQueue(id=printer.id, printer_id=printer.id))
        source = QueueSource(sha256="a" * 64, size_bytes=1, relative_path="queue-spool/objects/a.gcode", format="gcode")
        db.add(source)
        await db.flush()
        item = PrintQueueItem(
            queue_id=printer.id, queue_source_id=source.id, position=0, batch_id="same-batch", status="pending"
        )
        db.add(item)
        await db.commit()
        return account, session, item.id, printer.id, source.id


async def test_key_new_clone_uses_current_immutable_owner(test_engine):
    from backend.app.core.auth_lifetime import capture_key

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    account, _, item_id, _, _ = await clone_setup(factory)
    async with factory() as db:
        key = APIKey(
            name="queue",
            key_hash="only-captured-descriptor-needed",
            key_prefix="bd_proof",
            user_id=account.user_id,
            owner_identity=account.account_identity,
            contract_version=1,
            can_queue=True,
        )
        db.add(key)
        await db.flush()
        capture = capture_key(key)
        await db.commit()
    async with factory() as db:
        clone = await queue_ops.clone_item(db, item_id, created_by_id=None, authority=capture)
        assert clone.created_by_id == account.user_id


async def test_clone_batch_changed_membership_cannot_add_unpinned_work(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    account, session, _, queue_id, source_id = await clone_setup(factory)
    original = queue_ops.reusing_sources
    injected = []

    @asynccontextmanager
    async def concurrent_new_member(db, *args, **kwargs):
        # A second writer legitimately adds a sibling after the first request's
        # preflight. Its source/visibility was absent from the first guard set.
        await db.commit()
        async with factory() as other:
            row = PrintQueueItem(
                queue_id=queue_id,
                queue_source_id=source_id,
                position=1,
                batch_id="same-batch",
                status="pending",
                created_by_id=None,
            )
            other.add(row)
            await other.commit()
            injected.append(row.id)
        async with original(db, *args, **kwargs) as values:
            yield values

    monkeypatch.setattr(queue_ops, "reusing_sources", concurrent_new_member)
    async with factory() as db:
        with pytest.raises(queue_sources.SourceUnreadable, match="clone batch changed"):
            await queue_ops.clone_batch(db, "same-batch", created_by_id=account.user_id, authority=session)
        await db.rollback()
    async with factory() as db:
        assert len(injected) == 1
        assert await db.scalar(select(func.count()).select_from(PrintQueueItem)) == 2
