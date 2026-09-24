"""The contract of ``retire_paho_client`` itself (upstream #3068).

The callers' behaviour -- nobody on the event loop waits for a wedged network
thread -- is pinned in ``tests/unit/services/test_mqtt_client_retirement_3068.py``.
This file pins what the helper promises on its own: callbacks go inline, the
DISCONNECT still goes out, a slow retirement is said out loud, and running out
of threads does not skip the DISCONNECT.
"""

import logging
import threading
from unittest.mock import MagicMock, patch

from backend.app.utils import paho_teardown
from backend.app.utils.paho_teardown import retire_paho_client


class _Wedged:
    """``loop_stop`` holds until released, like a thread stuck in ``do_handshake``."""

    def __init__(self):
        self.released = threading.Event()
        self.disconnect_called = threading.Event()
        self.on_connect = self.on_disconnect = self.on_subscribe = self.on_message = "sentinel"

    def disconnect(self):
        self.disconnect_called.set()

    def loop_stop(self):
        self.released.wait(timeout=10)


def test_callbacks_are_gone_before_it_returns():
    wedged = _Wedged()
    try:
        retire_paho_client(wedged, "TESTSERIAL")
        assert (wedged.on_connect, wedged.on_disconnect, wedged.on_subscribe, wedged.on_message) == (
            None,
            None,
            None,
            None,
        )
    finally:
        wedged.released.set()


def test_the_disconnect_still_goes_out():
    wedged = _Wedged()
    try:
        retire_paho_client(wedged, "TESTSERIAL")
        assert wedged.disconnect_called.wait(timeout=5)
    finally:
        wedged.released.set()


def test_the_thread_is_named_for_the_connection():
    """A thread dump is how the next stuck one gets recognised; an anonymous
    ``Thread-7`` says nothing."""
    wedged = _Wedged()
    try:
        thread = retire_paho_client(wedged, "TESTSERIAL")
        assert thread is not None
        assert thread.name == "mqtt-retire-TESTSERIAL"
        assert thread.daemon, "a wedged teardown must not keep the process from exiting"
    finally:
        wedged.released.set()


def test_a_client_that_raises_on_teardown_is_still_let_go():
    exploding = MagicMock()
    exploding.disconnect.side_effect = RuntimeError("socket already gone")
    exploding.loop_stop.side_effect = RuntimeError("no thread")

    thread = retire_paho_client(exploding, "TESTSERIAL")
    thread.join(timeout=5)

    assert exploding.disconnect.called
    assert exploding.loop_stop.called


def test_a_slow_retirement_is_reported(monkeypatch, caplog):
    monkeypatch.setattr(paho_teardown, "_RETIRE_SLOW_SECONDS", 0.0)
    quick = MagicMock()

    with caplog.at_level(logging.WARNING, logger=paho_teardown.__name__):
        retire_paho_client(quick, "TESTSERIAL").join(timeout=5)

    assert "TESTSERIAL" in caplog.text
    assert "would not stop" in caplog.text


def test_a_fast_retirement_is_quiet(caplog):
    quick = MagicMock()

    with caplog.at_level(logging.WARNING, logger=paho_teardown.__name__):
        retire_paho_client(quick, "TESTSERIAL").join(timeout=5)

    assert caplog.text == ""


def test_out_of_threads_still_sends_the_disconnect(caplog):
    """⚠️ The DISCONNECT is what keeps the abandoned session from reconnecting
    and replaying an unacked ``project_file`` (#1136) -- it must not depend on
    the thread starting."""
    client = MagicMock()

    with (
        patch.object(paho_teardown.threading.Thread, "start", side_effect=RuntimeError("can't start new thread")),
        caplog.at_level(logging.ERROR, logger=paho_teardown.__name__),
    ):
        thread = retire_paho_client(client, "TESTSERIAL")

    assert thread is None
    client.disconnect.assert_called_once()
    client.loop_stop.assert_not_called()
    assert "Could not start the MQTT teardown thread" in caplog.text
