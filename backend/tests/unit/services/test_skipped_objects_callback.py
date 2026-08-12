"""``on_skipped_objects_changed`` fires when the PRINTER says an object is
being skipped — never when we merely asked for it.

The counter it feeds (``PrintArchive.defective_count``) is only as good as the
notification, so the question is which event deserves to be called a skip.

``skip_objects()`` used to record its own command into ``state.skipped_objects``
before the printer echoed it back. That made the state say "skipped" the instant
we asked, and it made the echo a no-op: the ``s_obj`` branch fires on a *diff*
against what we hold, and we had already written the answer into the thing it
diffs against. The notification still happened, from the send path — so these
tests passed while the state was, for a moment, a claim rather than a fact.

Firmware can decline: the object may already be finished, the print may have
ended between the click and the publish, or the plate may carry no object labels
to skip by. A declined skip counted as a defect and stayed counted, because the
correcting echo could no longer register as a change.

Now ``s_obj`` is the only writer, as it is in BS (``m_partskip_ids`` is filled
from ``s_obj`` and from nothing else). The echo of our own skip is a change
again, so it notifies — later than before, and only when it is true.
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


def test_asking_is_not_skipping(client):
    """The command goes out; the state does not move until the printer agrees."""
    seen: list[list] = []
    client.on_skipped_objects_changed = seen.append

    assert client.skip_objects([941, 942]) is True

    assert client.state.skipped_objects == []
    assert seen == []


def test_our_own_skip_notifies_when_the_printer_confirms_it(client):
    """The case the old optimistic write was there to cover — and which the
    echo covers by itself once that write is gone."""
    seen: list[list] = []
    client.on_skipped_objects_changed = seen.append

    client.skip_objects([941, 942])
    client._update_state({"s_obj": [941, 942]})

    assert client.state.skipped_objects == [941, 942]
    assert seen == [[941, 942]]


def test_a_declined_skip_never_counts(client):
    """Firmware ignoring the request must not leave a defect on the archive.

    Under the optimistic write this was unrecoverable: the id was already in
    state, so the printer's ``s_obj`` — still empty — was not a diff and could
    not take it back out.
    """
    seen: list[list] = []
    client.on_skipped_objects_changed = seen.append

    client.skip_objects([941])
    client._update_state({"s_obj": []})  # the printer skipping nothing

    assert client.state.skipped_objects == []
    assert seen == []


def test_printer_reported_skips_notify(client):
    """Skips made on the printer's screen arrive only through ``s_obj``."""
    seen: list[list] = []
    client.on_skipped_objects_changed = seen.append

    client._update_state({"s_obj": [941]})

    assert seen == [[941]]


def test_a_repeated_echo_does_not_fire_again(client):
    """Belt and braces for the counter: it is written as ``max(current,
    len(list))`` precisely so a repeat carrying the same list cannot inflate it,
    but the repeat should not reach it at all."""
    seen: list[list] = []
    client.on_skipped_objects_changed = seen.append

    client._update_state({"s_obj": [941]})
    client._update_state({"s_obj": [941]})

    assert seen == [[941]]


def test_the_whole_list_is_passed_not_the_delta(client):
    """Consumers record a count, so they need the total, not what just changed."""
    seen: list[list] = []
    client.on_skipped_objects_changed = seen.append

    client._update_state({"s_obj": [941]})
    client._update_state({"s_obj": [941, 942]})

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
