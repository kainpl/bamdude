"""Waiting for a macro across a flaky MQTT link.

Operator-reported: a swap A1 Mini failed a print with

    Swap macro 'A1 Mini. STL Edition. Start Sequence' failed: Printer disconnected
    during macro execution

The printer almost certainly ran the sequence. What actually happened is that
the health-check polling ``client.state.connected`` every 0.5 s caught a
momentary MQTT drop, and a swap sequence runs for tens of seconds — so the
window is wide open. That farm's support log shows **11 disconnects and 20
reconnects in 9h24m**, four of them ``rc=Unspecified error``: these links flap
as a matter of course.

The old behaviour turned any flap into a failed print, and a failed print arms
the plate-clear gate — which is how a printer ended up silently refusing to
take queue items.

Two changes, both pinned here:

* a disconnect is tolerated for a grace period, so a reconnect inside it is a
  non-event;
* if contact is not restored, the outcome says *contact was lost and the macro's
  state is unknown* rather than *the macro failed*. It still stops the print —
  proceeding without knowing whether the bed was prepared is the one outcome
  worse than a false failure — but the operator is pointed at the network
  instead of at their macro.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.printer_manager import MACRO_DISCONNECT_GRACE_SECONDS, PrinterManager


def _client(connected: bool = True):
    return SimpleNamespace(state=SimpleNamespace(connected=connected))


async def _run(manager, printer_id=1):
    with patch(
        "backend.app.services.macro_executor.send_macro_and_await_ack",
        AsyncMock(return_value=(True, "ack")),
    ):
        return await manager.execute_macro_and_wait(printer_id, "M400", "Start Sequence")


@pytest.mark.asyncio
async def test_a_macro_that_completes_normally_succeeds():
    manager = PrinterManager()
    client = _client()
    manager._clients[1] = client

    async def complete_soon():
        await asyncio.sleep(0.05)
        event, result = manager._macro_waiters[1]
        result["status"] = "completed"
        result["message"] = "done"
        event.set()

    task = asyncio.create_task(complete_soon())
    ok, msg = await _run(manager)
    await task

    assert ok is True


@pytest.mark.asyncio
async def test_a_brief_flap_does_not_fail_the_macro():
    """The regression. A drop that heals must not end the print."""
    manager = PrinterManager()
    client = _client()
    manager._clients[1] = client

    async def flap_then_complete():
        await asyncio.sleep(0.05)
        client.state.connected = False  # the blip
        await asyncio.sleep(0.6)  # longer than one 0.5s poll
        client.state.connected = True  # ...and it heals
        await asyncio.sleep(0.6)
        event, result = manager._macro_waiters[1]
        result["status"] = "completed"
        result["message"] = "done"
        event.set()

    task = asyncio.create_task(flap_then_complete())
    ok, msg = await _run(manager)
    await task

    assert ok is True, f"a healed flap must not fail the macro (got {msg!r})"


@pytest.mark.asyncio
async def test_a_macro_completing_while_offline_still_counts():
    """The completion event is the authority, not the socket. If the printer
    reports the macro done and drops immediately after, that is a success."""
    manager = PrinterManager()
    client = _client()
    manager._clients[1] = client

    async def complete_then_drop():
        await asyncio.sleep(0.05)
        event, result = manager._macro_waiters[1]
        result["status"] = "completed"
        result["message"] = "done"
        client.state.connected = False
        event.set()

    task = asyncio.create_task(complete_then_drop())
    ok, _ = await _run(manager)
    await task

    assert ok is True


@pytest.mark.asyncio
async def test_a_lasting_disconnect_reports_unknown_not_failure(monkeypatch):
    """Contact never came back: stop, but say what actually happened."""
    monkeypatch.setattr("backend.app.services.printer_manager.MACRO_DISCONNECT_GRACE_SECONDS", 1.0)
    manager = PrinterManager()
    manager._clients[1] = _client(connected=False)

    ok, msg = await _run(manager)

    assert ok is False
    # "failed" would send the operator to read their macro; the problem is the link.
    assert "unknown" in msg.lower()
    assert "contact" in msg.lower() or "connection" in msg.lower()


@pytest.mark.asyncio
async def test_the_waiter_is_always_removed(monkeypatch):
    """A leaked waiter would make the next macro on this printer resolve against
    a stale event."""
    monkeypatch.setattr("backend.app.services.printer_manager.MACRO_DISCONNECT_GRACE_SECONDS", 0.2)
    manager = PrinterManager()
    manager._clients[1] = _client(connected=False)

    await _run(manager)

    assert 1 not in manager._macro_waiters


def test_the_grace_period_is_thirty_seconds():
    """Chosen with the operator: long enough to ride out the observed flaps,
    short enough that a genuinely dead printer is not waited on for minutes."""
    assert MACRO_DISCONNECT_GRACE_SECONDS == 30.0
