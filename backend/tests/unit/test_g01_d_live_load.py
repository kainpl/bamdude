"""Disposable-farm load oracles for batching, projection reuse and live owners.

No network/device or raw-credential hashing is involved. Session/key sockets
exercise the real manager; LL camera handles exercise the same real coordinator
without inventing an LL WebSocket door. Optional evidence output is opt-in.
"""

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from collections import Counter
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.core import auth_runtime, database, websocket
from backend.app.core.auth_lifetime import FrozenAuthority, capture_key, capture_long_lived, utcnow
from backend.app.models.api_key import APIKey
from backend.app.models.auth_session import AuthSession
from backend.app.models.group import Group
from backend.app.models.long_lived_token import LongLivedToken
from backend.app.models.printer import Printer
from backend.app.models.user import User
from backend.app.services import live_authority as module, live_projection

pytestmark = pytest.mark.asyncio


def _pins():
    return {
        name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
        for name in (
            "backend/app/services/live_authority.py",
            "backend/app/services/live_projection.py",
            "backend/app/core/websocket.py",
            "backend/app/services/live_media.py",
        )
    }


def _report(section, value):
    output = os.getenv("BAMDUDE_D_LOAD_REPORT")
    if output:
        path = Path(output)
        existing = json.loads(path.read_text("utf-8")) if path.exists() else {}
        existing[section] = value
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((json.dumps(existing, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


@pytest.fixture
async def farm(test_engine, monkeypatch):
    runtime = module.LiveAuthority()
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(module, "live_authority", runtime)
    monkeypatch.setattr(websocket, "live_authority", runtime)
    monkeypatch.setattr(live_projection, "live_authority", runtime)
    monkeypatch.setattr(database, "async_session", factory)
    auth_runtime.auth_runtime.open_admission()
    yield runtime, factory, test_engine
    assert await runtime.shutdown(timeout=3), "load probe left a real live owner charged"


async def _seed(factory, count=16, *, integrations=True):
    """Insert fresh internally coherent current-contract credentials, no sign-in."""
    now = utcnow()
    base = [
        "websocket:connect",
        "printers:read",
        "camera:view",
        "queue:read_all",
        "archives:read_all",
        "library:read_all",
    ]
    varying = ["orders:read", "products:read", "stock:read", "inventory:read"]
    async with factory() as db:
        groups = [
            Group(name=f"load-class-{i}", permissions=base + [p for bit, p in enumerate(varying) if i & (1 << bit)])
            for i in range(16)
        ]
        users = [
            User(
                username=f"load-owner-{i}",
                password_hash="no-signin-in-this-fixture",
                role="user",
                is_active=True,
                groups=[groups[i % 16]],
            )
            for i in range(count)
        ]
        printers = [
            Printer(
                name=f"load-printer-{i}",
                serial_number=f"LOAD-{i}",
                ip_address=f"192.0.2.{i + 1}",
                access_code="synthetic",
                model="P1S",
            )
            for i in range(64)
        ]
        db.add_all([*groups, *users, *printers])
        await db.flush()
        sessions = [
            AuthSession(
                sid=uuid.uuid4().hex,
                user_id=user.id,
                account_identity=user.account_identity,
                generation=user.session_generation,
                auth_source="local",
                remember_me=False,
                created_at=now,
                expires_at=now + timedelta(hours=1),
            )
            for user in users
        ]
        db.add_all(sessions)
        keys, tokens = [], []
        if integrations:
            for i in range(8):
                owner = users[8 + i]
                keys.append(
                    APIKey(
                        name=f"load-key-{i}",
                        key_hash=f"synthetic-no-hash-{i}",
                        key_prefix=f"loadkey{i}",
                        enabled=True,
                        user_id=owner.id,
                        owner_identity=owner.account_identity,
                        contract_version=1,
                        can_read_status=True,
                        printer_ids=[printer.id for printer in printers[i * 8 : i * 8 + 8]],
                    )
                )
                owner = users[i]
                tokens.append(
                    LongLivedToken(
                        name=f"load-camera-{i}",
                        user_id=owner.id,
                        owner_identity=owner.account_identity,
                        contract_version=1,
                        lookup_prefix=f"loadll{i:02}",
                        secret_hash=f"synthetic-never-verified-{i}",
                        scope="camera_stream",
                        expires_at=now + timedelta(days=1),
                    )
                )
            db.add_all([*keys, *tokens])
        await db.commit()
        deadline = time.monotonic() + 120
        descriptors = [
            module.LiveDescriptor(
                FrozenAuthority("session", user.account_identity, user.id, user.session_generation, session.sid),
                ("websocket:connect",),
                expires=deadline,
            )
            for user, session in zip(users, sessions, strict=True)
        ]
        key_descriptors = [
            module.LiveDescriptor(capture_key(key), ("websocket:connect",), expires=deadline, key_hash=key.key_hash)
            for key in keys
        ]
        ll_descriptors = [
            module.LiveDescriptor(
                capture_long_lived(token), ("camera:view",), expires=deadline, long_lived_scope="camera_stream"
            )
            for token in tokens
        ]
        return {
            "session": descriptors,
            "key": key_descriptors,
            "long_lived": ll_descriptors,
            "printers": [(printer.id, printer.connector_identity) for printer in printers],
            "sid": sessions[0].sid,
            "key_identity": keys[0].connector_identity if keys else None,
            "token_identity": tokens[0].token_identity if tokens else None,
        }


class Queries:
    def __init__(self, engine):
        self.engine = engine
        self.rows = []

    def capture(self, _conn, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            self.rows.append(
                {
                    "tables": re.findall(r"\bFROM\s+([\w]+)", statement, re.I),
                    "bind_count": len(parameters),
                    "account_identity_in": "users.account_identity IN" in statement,
                    "task": asyncio.current_task().get_name(),
                    "sql_sha256": hashlib.sha256(statement.encode()).hexdigest(),
                }
            )

    def __enter__(self):
        event.listen(self.engine.sync_engine, "before_cursor_execute", self.capture)
        return self

    def __exit__(self, *_args):
        event.remove(self.engine.sync_engine, "before_cursor_execute", self.capture)


def _descriptors(seed, repetitions):
    ws = [descriptor for descriptor in [*seed["session"][:8], *seed["key"]] for _ in range(repetitions)]
    ll = []
    for i, descriptor in enumerate(seed["long_lived"]):
        for j in range(repetitions // 2):
            pid, identity = seed["printers"][i * 8 + j % 8]
            ll.append(replace(descriptor, printer_id=pid, printer_identity=identity))
    return ws, ll


class Browser:
    def __init__(self, class_index, *, blocked=False):
        self.class_index = class_index
        self.starts, self.sent, self.closed = [], [], []
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()

    async def accept(self):
        await asyncio.sleep(0)

    async def send_text(self, data):
        # This is the actual writer's ASGI send-start observation, before an
        # already-entered send may block in the server/client transport.
        self.starts.append(time.monotonic())
        await self.release.wait()
        self.sent.append(json.loads(data))
        await asyncio.sleep(0)

    async def close(self, code=1000):
        self.closed.append(code)


async def _until(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.001)


async def _run_viewers(runtime, factory, engine, seed, repetitions, monkeypatch, *, revoke=False):
    manager = websocket.ConnectionManager()
    ws_descriptors, ll_descriptors = _descriptors(seed, repetitions)
    browsers = [Browser(i // repetitions, blocked=i == 0) for i in range(len(ws_descriptors))]
    handles = [runtime.register(descriptor) for descriptor in ws_descriptors]
    ll_handles = [runtime.register(descriptor) for descriptor in ll_descriptors]
    loop_lag = []

    async def heartbeat():
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            loop_lag.append(max(0, now - last - 0.01))
            last = now

    beat = asyncio.create_task(heartbeat())
    held_release = asyncio.Event()
    try:
        started = time.monotonic()
        await asyncio.wait_for(
            asyncio.gather(
                *(
                    manager.connect(browser, authority=descriptor.capture, live_handle=handle)
                    for browser, descriptor, handle in zip(browsers, ws_descriptors, handles, strict=True)
                ),
                *(runtime.admit(handle) for handle in ll_handles),
            ),
            5,
        )
        admission = time.monotonic() - started
        classes = {(handle.entitlements, handle.descriptor.capture.kind) for handle in handles}
        assert len(classes) == 16
        counts = Counter()
        original_shape, original_dumps = live_projection.shape, websocket.json.dumps

        def shape(*args, **kwargs):
            counts["shape"] += 1
            return original_shape(*args, **kwargs)

        def dumps(*args, **kwargs):
            counts["serialization"] += 1
            return original_dumps(*args, **kwargs)

        before = time.monotonic()
        producer_task = asyncio.current_task().get_name()
        with monkeypatch.context() as scoped, Queries(engine) as queries:
            scoped.setattr(live_projection, "shape", shape)
            scoped.setattr(websocket.json, "dumps", dumps)
            for pid, _identity in seed["printers"]:
                await manager.send_printer_status(pid, {"progress": pid, "state": "RUNNING"})
            enqueue = time.monotonic() - before
            await _until(
                lambda: all(
                    len(browser.sent) == (64 if i < 8 * repetitions else 8)
                    for i, browser in enumerate(browsers)
                    if i != 0
                )
            )
        delivered = time.monotonic() - before
        assert browsers[0].starts and not browsers[0].sent
        result = {
            "ws_viewers": len(browsers),
            "ll_camera_authority_handles": len(ll_handles),
            "total_live_handles": len(handles) + len(ll_handles),
            "printers": 64,
            "projection_classes": len(classes),
            "families": dict(Counter(h.descriptor.capture.kind for h in [*handles, *ll_handles])),
            "admission_seconds": admission,
            "enqueue_64_printers_seconds": enqueue,
            "all_healthy_delivered_seconds": delivered,
            "shape_calls": counts["shape"],
            "serialization_calls": counts["serialization"],
            "broadcast_selects": sum(row["task"] == producer_task for row in queries.rows),
            "background_selects_during_broadcast": sum(row["task"] != producer_task for row in queries.rows),
            "per_class_healthy_delivered_min": {
                str(i): min(len(b.sent) for b in browsers if b.class_index == i and b is not browsers[0])
                for i in range(16)
            },
            "per_viewer": [
                {
                    "class": browser.class_index,
                    "delivered": len(browser.sent),
                    "send_starts": len(browser.starts),
                    "first_send_start_seconds": browser.starts[0] - before if browser.starts else None,
                    "last_send_start_seconds": browser.starts[-1] - before if browser.starts else None,
                }
                for browser in browsers
            ],
            "max_loop_lag_seconds": max(loop_lag, default=0),
        }
        assert counts["shape"] == 16 * 64
        assert counts["serialization"] == 576  # eight unrestricted + one scoped key class per printer
        assert result["broadcast_selects"] == 0

        if revoke:
            targets = [
                browser
                for browser, descriptor in zip(browsers, ws_descriptors, strict=True)
                if descriptor.capture.sid == seed["sid"] or descriptor.capture.key_identity == seed["key_identity"]
            ]
            target_handles = [
                handle
                for handle in handles
                if handle.descriptor.capture.sid == seed["sid"]
                or handle.descriptor.capture.key_identity == seed["key_identity"]
            ]
            target_ll = [
                handle for handle in ll_handles if handle.descriptor.capture.token_identity == seed["token_identity"]
            ]
            # Native SQL bypasses postcommit ORM hints, representing another
            # process/CLI. Only the periodic fresh batch can discover revocation.
            async with engine.begin() as conn:
                await conn.execute(
                    update(AuthSession).where(AuthSession.sid == seed["sid"]).values(revoked_at=utcnow())
                )
                await conn.execute(
                    update(APIKey).where(APIKey.connector_identity == seed["key_identity"]).values(enabled=False)
                )
                await conn.execute(
                    update(LongLivedToken)
                    .where(LongLivedToken.token_identity == seed["token_identity"])
                    .values(revoked_at=utcnow())
                )
            committed = time.monotonic()

            async def publish():
                while True:
                    await manager.send_printer_status(seed["printers"][0][0], {"progress": 99, "state": "RUNNING"})
                    await asyncio.sleep(0.02)

            publisher = asyncio.create_task(publish())
            try:
                await asyncio.wait_for(asyncio.gather(*(h.stop.wait() for h in [*target_handles, *target_ll])), 5)
                retired = time.monotonic()
                after = [stamp - committed for browser in targets for stamp in browser.starts if stamp >= committed]
                result["revocation"] = {
                    "target_ws": len(targets),
                    "target_ll": len(target_ll),
                    "retirement_seconds": retired - committed,
                    "last_new_send_after_commit_seconds": max(after, default=0),
                    "new_send_starts_after_commit": len(after),
                    "already_entered_stalled_sends": 1,
                    "close_codes": sorted({h.code for h in [*target_handles, *target_ll]}),
                    "send_start_offsets_after_commit_seconds": after,
                }
                assert max(after, default=0) <= 5
                before_counts = [len(browser.starts) for browser in targets]
                await manager.send_printer_status(seed["printers"][0][0], {"progress": 999, "state": "RUNNING"})
                await asyncio.sleep(0.02)
                assert before_counts == [len(browser.starts) for browser in targets]
                assert all(h.deliverable() for h in handles if h not in target_handles)
            finally:
                publisher.cancel()
                await asyncio.gather(publisher, return_exceptions=True)

            entered = asyncio.Event()
            original_read = runtime._read

            async def held(batch):
                outcome = await original_read(batch)
                entered.set()
                while not held_release.is_set():
                    try:
                        await held_release.wait()
                    except asyncio.CancelledError:
                        pass
                return outcome

            with monkeypatch.context() as scoped:
                scoped.setattr(runtime, "_read", held)
                pending = runtime.register(seed["session"][1])
                waiter = asyncio.create_task(runtime.admit(pending))
                try:
                    await asyncio.wait_for(entered.wait(), 3)
                    actual = runtime._check
                    waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
                    await asyncio.sleep(module.CHECK_SECONDS + 0.05)
                    assert actual in runtime.tasks and not actual.done()
                    refused = not await runtime.drain(timeout=0.02)
                    result["cancelled_check"] = {
                        "actual_owned_tasks": len(runtime.tasks),
                        "actual_check_done_before_release": actual.done(),
                        "restore_drain_refused": refused,
                        "barrier": "after actual DB read; cancellation-resistant continuation before proof publication",
                    }
                    assert refused
                finally:
                    held_release.set()
                    await asyncio.gather(waiter, return_exceptions=True)
                    if runtime._check is not None:
                        await asyncio.gather(runtime._check, return_exceptions=True)
                    runtime.release(pending)
        result["max_loop_lag_seconds"] = max(loop_lag, default=0)
        result["loop_lag_samples_seconds"] = loop_lag
        result["loop_lag_budget_seconds"] = 0.5
        result["loop_lag_budget_met"] = result["max_loop_lag_seconds"] < 0.5
        # An unrelated CI worker can suspend this process. Keep the private
        # send boundary mandatory above; enforce the capacity/lag budget in
        # the separately recorded, controlled performance run only.
        if os.getenv("BAMDUDE_D_LOAD_ENFORCE_LAG") == "1":
            assert result["loop_lag_budget_met"]
        return result
    finally:
        held_release.set()
        browsers[0].release.set()
        beat.cancel()
        await asyncio.gather(beat, return_exceptions=True)
        # Real routes call disconnect in finally even if their writer retired
        # and removed its outbox already. These direct manager callers own that
        # same duty; shutdown alone enumerates only still-running writers.
        await asyncio.gather(*(manager.disconnect(browser) for browser in browsers))
        await manager.shutdown()
        for handle in ll_handles:
            runtime.release(handle)
        assert not manager._outboxes and not manager._live_handles


async def test_load_128_to_256_viewers_keeps_family_queries_and_projection_work_constant(farm, monkeypatch):
    pins = _pins()
    runtime, factory, engine = farm
    seed = await _seed(factory)
    reads = []
    for repetitions in (8, 16):
        ws, ll = _descriptors(seed, repetitions)
        batch = [module.LiveHandle(descriptor, accepted_at=0) for descriptor in [*ws, *ll]]
        started = time.monotonic()
        with Queries(engine) as queries:
            outcomes, _metadata = await runtime._read(batch)
        assert len(outcomes) == len(batch) and all(not isinstance(row, int) for row in outcomes.values())
        reads.append(
            {
                "handles": len(batch),
                "select_count": len(queries.rows),
                "queries": queries.rows,
                "elapsed_seconds": time.monotonic() - started,
            }
        )
    assert reads[0]["select_count"] == reads[1]["select_count"]
    small = await _run_viewers(runtime, factory, engine, seed, 8, monkeypatch)
    large = await _run_viewers(runtime, factory, engine, seed, 16, monkeypatch, revoke=True)
    assert small["shape_calls"] == large["shape_calls"]
    assert small["serialization_calls"] == large["serialization_calls"]
    _report(
        "mixed_farm",
        {
            "source_pins_start": pins,
            "source_pins_end": _pins(),
            "family_reads": reads,
            "baseline": small,
            "doubled": large,
            "boundary": "Real SQLite coordinator/manager; fake ASGI socket transport; LL handles are camera authority only, no LL WS door or physical captures.",
        },
    )


async def test_diverse_identities_use_bounded_chunks_and_declared_caps(farm):
    pins = _pins()
    runtime, factory, engine = farm
    seed = await _seed(factory, count=513, integrations=False)
    handles = [module.LiveHandle(descriptor, accepted_at=0) for descriptor in seed["session"]]
    started = time.monotonic()
    with Queries(engine) as queries:
        outcomes, _ = await runtime._read(handles)
    read_elapsed = time.monotonic() - started
    assert len(outcomes) == 513 and all(not isinstance(row, int) for row in outcomes.values())
    assert max(row["bind_count"] for row in queries.rows) <= module.CHUNK_SIZE
    user_queries = [row for row in queries.rows if row["account_identity_in"]]
    session_queries = [row for row in queries.rows if row["tables"] == ["auth_sessions"]]
    assert len(user_queries) == len(session_queries) == 3
    # Batch-driven cap check exercises real SQL + publication, without starting
    # a second automatic scheduler beside the manually invoked batch.
    runtime.handles.update(handles[: module.MAX_PROJECTION_CLASSES + 1])
    await runtime._refresh(handles[: module.MAX_PROJECTION_CLASSES + 1])
    admitted = sum(handle.admitted for handle in runtime.handles)
    retired = sum(handle.retired for handle in runtime.handles)
    try:
        assert admitted == module.MAX_PROJECTION_CLASSES and retired == 1
        refused_class = [handle for handle in runtime.handles if handle.retired]
        assert refused_class[0].code == 1013
    finally:
        for handle in tuple(runtime.handles):
            runtime.release(handle)
    capacity_handles = []
    try:
        for _ in range(module.MAX_HANDLES):
            capacity_handles.append(runtime.register(seed["session"][0]))
        with pytest.raises(HTTPException) as caught:
            runtime.register(seed["session"][0])
        assert caught.value.status_code == 503
        assert len(runtime.handles) == module.MAX_HANDLES
    finally:
        for handle in capacity_handles:
            runtime.release(handle)
    _report(
        "diversity_and_caps",
        {
            "source_pins_start": pins,
            "source_pins_end": _pins(),
            "distinct_accounts_and_sids": 513,
            "select_count": len(queries.rows),
            "queries": queries.rows,
            "read_seconds": read_elapsed,
            "max_binds": max(row["bind_count"] for row in queries.rows),
            "chunk_size": module.CHUNK_SIZE,
            "projection_cap": module.MAX_PROJECTION_CLASSES,
            "admitted_classes": admitted,
            "excess_class_retired_1013": retired,
            "handle_cap": module.MAX_HANDLES,
            "excess_handle_status": caught.value.status_code,
        },
    )
