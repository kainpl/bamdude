"""Opt-in same-host ASGI benchmark for hot queue reads.

Run explicitly in both a baseline worktree and the candidate checkout. The
shared test fixtures keep MQTT/FTP/cloud disconnected and use disposable SQLite.
Numbers are diagnostic, not a production latency SLA.
"""

import asyncio
import json
import math
import os
import statistics
import time
from contextlib import AsyncExitStack
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event

from backend.app import main
from backend.app.core import auth
from backend.app.models.api_key import APIKey
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import queue_virtual


async def _lag_samples(stop: asyncio.Event, samples: list[float]) -> None:
    interval = 0.01
    due = time.perf_counter() + interval
    while not stop.is_set():
        await asyncio.sleep(max(0, due - time.perf_counter()))
        now = time.perf_counter()
        # Windows' event-loop timer can wake early; early samples would flood
        # the distribution with zeroes and hide a real long blocking gap.
        if now < due:
            continue
        samples.append((now - due) * 1000)
        due = now + interval


@pytest.mark.asyncio
async def test_hot_api_http_benchmark(async_client, db_session, monkeypatch):
    engine = db_session.bind.sync_engine
    old_lookup = auth.get_user_by_username
    lookup_count = 0

    async def counted_lookup(db, username):
        nonlocal lookup_count
        lookup_count += 1
        return await old_lookup(db, username)

    monkeypatch.setattr(auth, "get_user_by_username", counted_lookup)
    active_ids: set[int] = set()
    monkeypatch.setattr(
        queue_virtual.printer_manager,
        "get_status",
        lambda pid: SimpleNamespace(connected=True, state="RUNNING") if pid in active_ids else None,
    )
    monkeypatch.setattr(queue_virtual.printer_manager, "get_printer", lambda _pid: None)

    raw, key_hash, prefix = auth.generate_api_key()
    db_session.add(APIKey(name="benchmark-read", key_hash=key_hash, key_prefix=prefix, enabled=True))
    await db_session.commit()
    jwt_header = async_client.headers["Authorization"]

    sql_count = 0
    checkout_times: list[float] = []
    pool_wait_times: list[float] = []
    original_connect = engine.pool.connect

    def counted_connect():
        start = time.perf_counter()
        conn = original_connect()
        pool_wait_times.append((time.perf_counter() - start) * 1000)
        return conn

    def count_sql(_conn, _cursor, _statement, _params, _context, _many):
        nonlocal sql_count
        sql_count += 1

    def on_checkout(_dbapi, record, _proxy):
        record.info["hot_bench_checkout"] = time.perf_counter()

    def on_checkin(_dbapi, record):
        start = record.info.pop("hot_bench_checkout", None)
        if start is not None:
            checkout_times.append((time.perf_counter() - start) * 1000)

    async with AsyncExitStack() as stack:
        clients = [
            await stack.enter_async_context(AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test"))
            for _ in range(10)
        ]
        seeded = 0
        counts = tuple(int(value) for value in os.getenv("HOT_API_BENCH_COUNTS", "1,10,46,100").split(","))
        for count in counts:
            new_printers = [
                Printer(
                    name=f"Hot API {i}",
                    serial_number=f"HOTAPI{i:09d}",
                    ip_address=f"192.0.2.{i + 1}",
                    access_code="12345678",
                    model="P1S",
                    is_active=True,
                )
                for i in range(seeded, count)
            ]
            db_session.add_all(new_printers)
            await db_session.flush()
            for printer in new_printers:
                db_session.add(PrinterQueue(id=printer.id, printer_id=printer.id))
                db_session.add(PrintQueueItem(queue_id=printer.id, status="pending", position=0))
                active_ids.add(printer.id)
            await db_session.commit()
            seeded = count

            for credential, value in (("jwt", jwt_header), ("key", f"Bearer {raw}")):
                for status in ("pending", "printing", "all"):
                    url = "/api/v1/queue/" + (f"?status={status}" if status != "all" else "")
                    # Exclude one-time setup-gate initialization from the read.
                    warm = await clients[0].get(url, headers={"Authorization": value})
                    assert warm.status_code == 200, warm.text

                    sql_count = 0
                    lookup_count = 0
                    checkout_times.clear()
                    pool_wait_times.clear()
                    lag: list[float] = []
                    stop = asyncio.Event()
                    monitor = asyncio.create_task(_lag_samples(stop, lag))
                    started = time.perf_counter()
                    event.listen(engine, "before_cursor_execute", count_sql)
                    event.listen(engine.pool, "checkout", on_checkout)
                    event.listen(engine.pool, "checkin", on_checkin)
                    monkeypatch.setattr(engine.pool, "connect", counted_connect)
                    try:

                        async def one(client, request_url=url, credential_value=value):
                            t0 = time.perf_counter()
                            response = await client.get(request_url, headers={"Authorization": credential_value})
                            return response, (time.perf_counter() - t0) * 1000

                        results = await asyncio.gather(*(one(client) for client in clients))
                    finally:
                        monkeypatch.setattr(engine.pool, "connect", original_connect)
                        event.remove(engine, "before_cursor_execute", count_sql)
                        event.remove(engine.pool, "checkout", on_checkout)
                        event.remove(engine.pool, "checkin", on_checkin)
                        stop.set()
                        await monitor

                    responses, wall = zip(*results, strict=True)
                    assert all(response.status_code == 200 for response in responses), [
                        (response.status_code, response.text[:200])
                        for response in responses
                        if response.status_code != 200
                    ]
                    samples = sorted(wall)
                    print(
                        "HOT_API_BENCH="
                        + json.dumps(
                            {
                                "printers": count,
                                "credential": credential,
                                "status": status,
                                "clients": len(clients),
                                "sql": sql_count,
                                "auth_user_lookups": lookup_count,
                                "body_bytes": sum(len(response.content) for response in responses),
                                "wall_p50_ms": round(statistics.median(samples), 2),
                                "wall_p95_ms": round(samples[math.ceil(0.95 * len(samples)) - 1], 2),
                                "batch_wall_ms": round((time.perf_counter() - started) * 1000, 2),
                                "loop_lag_p95_ms": round(sorted(lag)[math.ceil(0.95 * len(lag)) - 1], 2) if lag else 0,
                                "loop_lag_samples": len(lag),
                                "loop_lag_max_ms": round(max(lag), 2) if lag else 0,
                                "pool_wait_p95_ms": round(
                                    sorted(pool_wait_times)[math.ceil(0.95 * len(pool_wait_times)) - 1], 2
                                )
                                if pool_wait_times
                                else 0,
                                "checkout_hold_p95_ms": round(
                                    sorted(checkout_times)[math.ceil(0.95 * len(checkout_times)) - 1], 2
                                )
                                if checkout_times
                                else 0,
                            },
                            sort_keys=True,
                        )
                    )
