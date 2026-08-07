"""K-profile replies are correlated per request, not through one shared slot (upstream #1748).

The client used to keep a single ``_pending_kprofile_response`` Event, a single
``_kprofile_response_data`` and a single ``_expected_kprofile_nozzle``. Two
concurrent requests collided: the second overwrote the first's expectation and
Event, the first's reply arrived, failed the nozzle comparison, was discarded as
a broadcast, and the first caller timed out through all three retries — the
"Failed to get K-profiles after 3 attempts" logged next to a printer that had
answered correctly both times.

**This is a regression we shipped.** The spool PA-Profil picker was changed to
fetch every installed nozzle diameter in parallel, so a dual-nozzle printer now
issues two requests on one client every time that dialog opens. Before that the
collision needed two users at once.

The reply is matched on the ``sequence_id`` we send; the nozzle comparison
survives only as a fallback for firmware that does not echo one back — which is
exactly the mechanism that could not tell two requests apart, so it must never be
the primary.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


def _client() -> BambuMQTTClient:
    c = BambuMQTTClient.__new__(BambuMQTTClient)
    # ``topic_publish`` is a read-only property derived from the serial.
    c.serial_number = "01P00A000000000"
    c._sequence_id = 0
    c._kprofile_waiters = {}
    c._client = MagicMock()
    c._loop = None
    c.state = MagicMock()
    c.state.connected = True
    c.state.kprofiles = []
    c.on_kprofiles_changed = None
    c._maybe_notify_kprofiles_changed = lambda _profiles: None
    return c


def _reply(nozzle: str, seq_id: str | None, *, k: str = "0.020") -> dict:
    """A printer reply. The per-entry ``nozzle_diameter`` is deliberately absent —
    that is what the payload actually looks like on every non-H2D firmware."""
    payload: dict = {
        "nozzle_diameter": nozzle,
        "filaments": [
            {"cali_idx": 1, "filament_id": "GFA00", "name": "PLA", "k_value": k, "n_coef": "0.0"},
        ],
    }
    if seq_id is not None:
        payload["sequence_id"] = seq_id
    return payload


@pytest.mark.asyncio
class TestConcurrentRequests:
    async def test_two_nozzles_at_once_both_get_their_own_answer(self) -> None:
        """The exact shape the spool dialog produces on a dual-nozzle printer."""
        c = _client()
        c._loop = asyncio.get_running_loop()

        published: list[str] = []
        c._client.publish = lambda _topic, payload, qos=1: published.append(payload)

        async def answer_when_both_are_waiting() -> None:
            # Wait until both requests have registered, so the replies are
            # genuinely interleaved rather than serialised by timing.
            for _ in range(200):
                if len(c._kprofile_waiters) == 2:
                    break
                await asyncio.sleep(0.005)
            seq_ids = sorted(c._kprofile_waiters)
            # Answer in the WRONG order — the second request first — because
            # that is what the shared slot could not survive.
            c._handle_kprofile_response(_reply("0.6", seq_ids[1], k="0.028"))
            await asyncio.sleep(0)
            c._handle_kprofile_response(_reply("0.4", seq_ids[0], k="0.042"))

        results = await asyncio.gather(
            c.get_kprofiles(nozzle_diameter="0.4", timeout=3.0),
            c.get_kprofiles(nozzle_diameter="0.6", timeout=3.0),
            answer_when_both_are_waiting(),
        )

        assert [p.k_value for p in results[0]] == ["0.042"], "the 0.4 caller got the 0.6 answer or timed out"
        assert [p.k_value for p in results[1]] == ["0.028"]

    async def test_each_request_sends_and_is_keyed_by_its_own_sequence_id(self) -> None:
        c = _client()
        c._loop = asyncio.get_running_loop()

        async def answer() -> None:
            for _ in range(200):
                if c._kprofile_waiters:
                    break
                await asyncio.sleep(0.005)
            seq_id = next(iter(c._kprofile_waiters))
            assert seq_id in c._client.publish.call_args[0][1], "the key must be the id we published"
            c._handle_kprofile_response(_reply("0.4", seq_id))

        await asyncio.gather(c.get_kprofiles(nozzle_diameter="0.4", timeout=3.0), answer())

    async def test_the_registry_is_empty_afterwards(self) -> None:
        """Each caller pops only its own entry, so nothing leaks and nothing of a
        concurrent caller's is taken with it."""
        c = _client()
        c._loop = asyncio.get_running_loop()

        async def answer() -> None:
            for _ in range(200):
                if c._kprofile_waiters:
                    break
                await asyncio.sleep(0.005)
            c._handle_kprofile_response(_reply("0.4", next(iter(c._kprofile_waiters))))

        await asyncio.gather(c.get_kprofiles(nozzle_diameter="0.4", timeout=3.0), answer())
        assert c._kprofile_waiters == {}


@pytest.mark.asyncio
class TestFallbackAndNoise:
    async def test_a_reply_without_a_sequence_id_still_matches_on_nozzle(self) -> None:
        """Older firmware does not echo the id back. The nozzle comparison stays
        as the fallback so those printers keep working."""
        c = _client()
        c._loop = asyncio.get_running_loop()

        async def answer() -> None:
            for _ in range(200):
                if c._kprofile_waiters:
                    break
                await asyncio.sleep(0.005)
            c._handle_kprofile_response(_reply("0.4", None))

        results = await asyncio.gather(c.get_kprofiles(nozzle_diameter="0.4", timeout=3.0), answer())
        assert len(results[0]) == 1

    async def test_an_unsolicited_broadcast_does_not_wake_a_waiter(self) -> None:
        """The printer broadcasts 0.4mm profiles constantly. One that matches no
        waiter must update state and nothing else."""
        c = _client()
        c._loop = asyncio.get_running_loop()

        async def answer() -> None:
            for _ in range(200):
                if c._kprofile_waiters:
                    break
                await asyncio.sleep(0.005)
            # Not ours: different nozzle, unknown id.
            c._handle_kprofile_response(_reply("0.8", "999999", k="0.001"))
            await asyncio.sleep(0.05)
            assert not c._kprofile_waiters[next(iter(c._kprofile_waiters))][0].is_set()
            c._handle_kprofile_response(_reply("0.6", next(iter(c._kprofile_waiters)), k="0.028"))

        results = await asyncio.gather(c.get_kprofiles(nozzle_diameter="0.6", timeout=3.0), answer())
        assert [p.k_value for p in results[0]] == ["0.028"]

    async def test_a_broadcast_with_no_waiters_at_all_still_updates_state(self) -> None:
        c = _client()
        c._handle_kprofile_response(_reply("0.4", None, k="0.033"))
        assert [p.k_value for p in c.state.kprofiles] == ["0.033"]
