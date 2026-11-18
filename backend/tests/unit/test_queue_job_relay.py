"""``queue/job_added`` follows the fact -- a pending queue row that was committed -- not a door.

Two doors published the event and every other one was silent (AutoQueue handing a job to a printer,
a Virtual Printer, Retry / Clone, the Telegram scenes). The session hooks in ``queue_job_relay`` note
each pending ``PrintQueueItem`` at flush and hand the ids to the relay on the root commit.
"""

from __future__ import annotations

import asyncio
import ssl
from unittest.mock import MagicMock

import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import (
    mqtt_relay as relay_module,
    queue_job_relay,  # noqa: F401 -- registers the session hooks
)
from backend.app.services.mqtt_relay import configure_broker_tls, mqtt_relay


@pytest.fixture
def announced(monkeypatch):
    seen: list[list[int]] = []

    async def record(item_ids):
        seen.append(list(item_ids))

    monkeypatch.setattr(mqtt_relay, "enabled", True)
    monkeypatch.setattr(mqtt_relay, "connected", True)
    monkeypatch.setattr(mqtt_relay, "on_queue_jobs_added", record)
    return seen


async def _settle():
    for _ in range(3):
        await asyncio.sleep(0)


async def test_a_committed_pending_row_is_announced_once(db_session, printer_factory, announced):
    printer = await printer_factory()
    item = PrintQueueItem(queue_id=printer.id, status="pending", position=1)
    db_session.add(item)
    await db_session.commit()
    await _settle()

    assert announced == [[item.id]]

    item.position = 2  # an edit of a row that already exists announces nothing
    await db_session.commit()
    await _settle()
    assert announced == [[item.id]]


async def test_a_row_that_only_holds_the_printer_is_not_a_queued_job(db_session, printer_factory, announced):
    printer = await printer_factory()
    db_session.add(PrintQueueItem(queue_id=printer.id, status="printing", position=0, origin="direct"))
    await db_session.commit()
    await _settle()

    assert announced == []


async def test_a_rolled_back_row_is_never_announced(db_session, printer_factory, announced):
    printer = await printer_factory()
    db_session.add(PrintQueueItem(queue_id=printer.id, status="pending", position=1))
    await db_session.flush()
    await db_session.rollback()
    await _settle()

    assert announced == []


async def test_a_savepoint_that_rolls_back_takes_its_row_with_it(db_session, printer_factory, announced):
    printer = await printer_factory()
    kept = PrintQueueItem(queue_id=printer.id, status="pending", position=1)
    db_session.add(kept)
    await db_session.flush()
    savepoint = await db_session.begin_nested()
    db_session.add(PrintQueueItem(queue_id=printer.id, status="pending", position=2))
    await db_session.flush()
    await savepoint.rollback()
    await db_session.commit()
    await _settle()

    assert announced == [[kept.id]]


async def test_nothing_is_scheduled_without_a_connected_relay(db_session, printer_factory, monkeypatch):
    called = MagicMock()
    monkeypatch.setattr(mqtt_relay, "connected", False)
    monkeypatch.setattr(mqtt_relay, "on_queue_jobs_added", called)
    printer = await printer_factory()
    db_session.add(PrintQueueItem(queue_id=printer.id, status="pending", position=1))
    await db_session.commit()
    await _settle()

    called.assert_not_called()


async def test_the_event_names_the_file_and_the_printer(db_session, printer_factory, monkeypatch):
    service = relay_module.MQTTRelayService()
    service.enabled = service.connected = True
    published = []
    service._publish = lambda topic, payload, retain=False: published.append((topic, payload))
    printer = await printer_factory(name="Farm P1S")
    item = PrintQueueItem(queue_id=printer.id, status="pending", position=1)
    db_session.add(item)
    await db_session.commit()

    await service.on_queue_jobs_added([item.id, 999999])

    ((topic, payload),) = published
    assert topic == "bamdude/queue/job_added"
    assert (payload["job_id"], payload["printer_id"], payload["printer_name"]) == (item.id, printer.id, "Farm P1S")


class TestBrokerCertificate:
    def test_unverified_is_the_default_so_a_self_signed_broker_keeps_working(self):
        client = MagicMock()
        configure_broker_tls(client, verify=False)
        client.tls_set.assert_called_once_with(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set.assert_called_once_with(True)

    def test_verification_checks_the_chain_and_the_host_name(self):
        client = MagicMock()
        configure_broker_tls(client, verify=True)
        client.tls_set.assert_called_once_with(cert_reqs=ssl.CERT_REQUIRED)
        client.tls_insecure_set.assert_called_once_with(False)
