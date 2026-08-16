"""A printer is never handed a command it has already executed.

**The incident, 2026-08-16.** `3DP-030-102` was eleven hours into a print when
its MQTT link dropped. Two seconds after it came back, the printer
acknowledged `gcode_line seq=20006` — a packet BamDude had published at 02:06
that morning carrying the plate-change macro. The bed was swept and the printer
went on extruding into the air. The same signature hit `3DP-030-201` earlier
the same day, twice, and its archive is recorded `failed` between the replays.

**Why it happens.** We publish with ``qos=1``: *at least once*. paho keeps the
promise by holding the message and re-sending until the broker PUBACKs, and the
broker here is the printer's own firmware, which loses PUBACKs (#1164).

⚠️ An unacknowledged packet is not a lost one — at 02:06 the sweep *executed*
and only the receipt went missing. paho cannot tell those apart. We can,
because the printer sends its own application-level acknowledgement.

⚠️ The deeper point: MQTT is built for telemetry, where a late message is still
true. A movement command is not state, it is an instruction, and it is only
valid in the situation it was issued for. We asked for "deliver eventually"
where this class of message needs "deliver now or not at all".
"""

from __future__ import annotations

from unittest.mock import MagicMock

from backend.app.services.bambu_mqtt import (
    BambuMQTTClient,
    _drain_outgoing,
    _drop_queued_message,
)


class _FakeClient:
    def __init__(self, mids):
        self._out_messages = {m: object() for m in mids}


def _client_with(queue: dict) -> BambuMQTTClient:
    """A real client whose paho half is a stand-in holding ``queue``."""
    c = BambuMQTTClient(ip_address="192.168.0.9", serial_number="01P00A000000000", access_code="00000000")
    fake = MagicMock()
    fake._out_messages = queue
    # paho's subscribe returns (result, mid); _on_connect unpacks it.
    fake.subscribe.return_value = (0, 1)
    c._client = fake
    c.state.connected = True
    return c


class TestTheDoorIntoPahosQueue:
    def test_drain_reports_how_many_it_removed(self):
        client = _FakeClient([1, 2, 3])

        assert _drain_outgoing(client) == 3
        assert client._out_messages == {}

    def test_drain_of_an_empty_queue_is_zero_not_an_error(self):
        assert _drain_outgoing(_FakeClient([])) == 0

    def test_drop_removes_only_the_named_message(self):
        client = _FakeClient([1, 2, 3])

        assert _drop_queued_message(client, 2) is True
        assert sorted(client._out_messages) == [1, 3]

    def test_dropping_an_unknown_mid_is_not_an_error(self):
        """Normal: the PUBACK usually arrives first and paho has already
        removed it, so most calls find nothing to do."""
        assert _drop_queued_message(_FakeClient([1]), 99) is False

    def test_a_paho_without_the_attribute_degrades_quietly(self):
        class Renamed:
            pass

        assert _drain_outgoing(Renamed()) == 0
        assert _drop_queued_message(Renamed(), 1) is False

    def test_the_real_paho_still_has_the_attribute(self):
        """⚠️ The canary, and the most important test in this file.

        If a paho upgrade renames the queue, both functions above degrade to
        doing nothing — and "nothing to discard" reads exactly like "no stale
        commands". The failure would be invisible and would hide the very bug
        this module exists to prevent, so it is asserted rather than assumed.
        """
        import paho.mqtt.client as mqtt

        c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)

        assert isinstance(getattr(c, "_out_messages", None), dict)


class TestOnConnect:
    def test_connecting_empties_the_queue(self):
        """The 16:42 incident in one line: anything still queued when the link
        returns has outlived the situation it was written for."""
        c = _client_with({7: object()})

        c._on_connect(c._client, None, None, 0, None)

        assert c._client._out_messages == {}, "a stale command survived the reconnect"

    def test_it_also_forgets_which_sequence_owned_which_packet(self):
        """The map is only useful for packets that still exist."""
        c = _client_with({7: object()})
        c._mid_by_sequence["20006"] = 7

        c._on_connect(c._client, None, None, 0, None)

        assert c._mid_by_sequence == {}

    def test_a_failed_connect_leaves_the_queue_alone(self):
        """rc != 0 is not a connection. Draining there would discard commands
        that never had their chance, on a client that may yet succeed."""
        c = _client_with({7: object()})

        c._on_connect(c._client, None, None, 5, None)

        assert 7 in c._client._out_messages


class TestThePrintersAcknowledgement:
    def test_it_withdraws_the_packet_from_the_retry_queue(self):
        """⚠️ The printer's ACK says "I received this and acted on it" — better
        evidence than a broker PUBACK, which only means the hop in between is
        content. After it, a retry is not caution; it is a second execution
        waiting for a disconnect.

        This is what the reconnect flush cannot cover: the X2D re-acknowledged
        one sequence seconds apart inside a single live session, with no
        disconnect at all.
        """
        c = _client_with({42: object()})
        c._client.publish.return_value = MagicMock(mid=42)

        c.send_gcode("M400 S5;")
        seq = str(c._sequence_id)

        c._process_message({"print": {"command": "gcode_line", "sequence_id": seq, "result": "success"}})

        assert c._client._out_messages == {}, "the acknowledged packet stayed retriable"

    def test_a_refused_command_is_withdrawn_too(self):
        """⚠️ "Device busy" is still an answer: the printer received it and
        declined. Re-delivering a refused movement command later is exactly the
        hazard — by then the machine is in a different state and may accept it.
        """
        c = _client_with({42: object()})
        c._client.publish.return_value = MagicMock(mid=42)

        c.send_gcode("M400 S5;")
        seq = str(c._sequence_id)

        c._process_message(
            {"print": {"command": "gcode_line", "sequence_id": seq, "result": "fail", "reason": "device busy"}}
        )

        assert c._client._out_messages == {}

    def test_an_ack_for_something_else_leaves_the_queue_alone(self):
        c = _client_with({42: object()})
        c._client.publish.return_value = MagicMock(mid=42)
        c.send_gcode("M400 S5;")

        c._process_message({"print": {"command": "gcode_line", "sequence_id": "999999", "result": "success"}})

        assert 42 in c._client._out_messages

    def test_the_sequence_map_does_not_grow_without_bound(self):
        """One entry per in-flight command, removed on its acknowledgement."""
        c = _client_with({})
        for n in range(5):
            c._client._out_messages[100 + n] = object()
            c._client.publish.return_value = MagicMock(mid=100 + n)
            c.send_gcode(f"M400 S{n};")
            c._process_message(
                {"print": {"command": "gcode_line", "sequence_id": str(c._sequence_id), "result": "success"}}
            )

        assert c._mid_by_sequence == {}
        assert c._client._out_messages == {}


class TestTheIncidentItself:
    def test_a_sweep_published_before_a_disconnect_is_never_delivered_after_it(self):
        """The reported failure, start to finish: publish the plate-change,
        lose the PUBACK, reconnect — and the printer must be sent nothing."""
        c = _client_with({})
        c._client.publish.return_value = MagicMock(mid=20006)
        c._client._out_messages[20006] = object()  # PUBACK never arrived

        c.send_gcode("M1002 gcode_claim_action : 0;\nG0 Y186.5 F2000;\n")

        # ...eleven hours pass, the link drops, paho reconnects itself.
        c._on_connect(c._client, None, None, 0, None)

        assert c._client._out_messages == {}, (
            "the plate-change macro survived the reconnect and would be re-delivered mid-print"
        )
