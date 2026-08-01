"""``on_skipped_objects_changed`` has to fire from both places a skip appears.

The counter it feeds (``PrintArchive.defective_count``) is only as good as the
notification, and there is one trap in the wiring that no amount of reading the
``s_obj`` branch alone reveals: ``skip_objects()`` records our own command into
``state.skipped_objects`` *before* the printer echoes it back, so by the time the
echo arrives the diff in that branch sees no change and stays silent. A listener
attached only there would hear about skips made on the printer's screen and never
about skips made from BamDude itself.
"""

from unittest.mock import Mock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


@pytest.fixture
def client():
    c = BambuMQTTClient(
        ip_address="192.168.1.100",
        serial_number="TEST123",
        access_code="12345678",
    )
    # Enough of a connection for skip_objects to get past its own guards.
    c._client = Mock()
    c.state.connected = True
    c.state.state = "RUNNING"
    return c


def test_our_own_skip_command_notifies(client):
    """The case the s_obj diff cannot catch on its own."""
    seen: list[list] = []
    client.on_skipped_objects_changed = seen.append

    assert client.skip_objects([941, 942]) is True

    assert seen == [[941, 942]]


def test_printer_reported_skips_notify(client):
    """Skips made on the printer's screen arrive only through ``s_obj``."""
    seen: list[list] = []
    client.on_skipped_objects_changed = seen.append

    client._update_state({"s_obj": [941]})

    assert seen == [[941]]


def test_the_printers_echo_of_our_own_skip_does_not_fire_again(client):
    """Our command already put the ids in state, so the echo is not a change.

    Belt and braces for the counter: it is written as ``max(current, len(list))``
    precisely so that a second notification carrying the same list cannot inflate
    it, but the echo should not even reach it.
    """
    seen: list[list] = []
    client.on_skipped_objects_changed = seen.append

    client.skip_objects([941])
    client._update_state({"s_obj": [941]})  # the printer confirming what we sent

    assert seen == [[941]], "the echo of our own skip must not notify a second time"


def test_re_skipping_the_same_object_does_not_fire(client):
    """A repeat call adds nothing to the list, so there is nothing to report."""
    seen: list[list] = []
    client.on_skipped_objects_changed = seen.append

    client.skip_objects([941])
    client.skip_objects([941])

    assert seen == [[941]]


def test_the_whole_list_is_passed_not_the_delta(client):
    """Consumers record a count, so they need the total, not what just changed."""
    seen: list[list] = []
    client.on_skipped_objects_changed = seen.append

    client.skip_objects([941])
    client.skip_objects([942])

    assert seen == [[941], [941, 942]]


def test_a_failing_consumer_cannot_break_the_mqtt_path(client):
    """This runs on the paho network thread — an exception there would take the
    callback chain down mid-status and leave the rest of the payload unparsed."""

    def explode(_skipped):
        raise RuntimeError("consumer is broken")

    client.on_skipped_objects_changed = explode

    assert client.skip_objects([941]) is True
    client._update_state({"s_obj": [941, 942]})  # must not raise either

    assert client.state.skipped_objects == [941, 942]
