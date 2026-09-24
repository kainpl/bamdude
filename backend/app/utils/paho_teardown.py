"""Letting go of a paho MQTT client without waiting for its network thread.

``Client.loop_stop()`` is two statements: set ``_thread_terminate``, then
``join()`` the network thread with no timeout. That thread reads the flag only
between iterations of ``loop_forever``, so it cannot read it while parked in
``reconnect()`` -> ``_ssl_wrap_socket()`` -> ``do_handshake()``. paho gives that
handshake the keepalive as its socket timeout -- 30 s for a printer -- and a
socket timeout is per operation, renewed by every byte the peer sends. A broker
that answers on its port but never finishes the handshake holds the join open
for as long as it keeps trickling; a silent one still holds it 30 s.

Whoever called ``loop_stop()`` waits that out, and in BamDude that caller was
the asyncio thread (upstream Bambuddy #3068: a printer 38 hours offline, still
answering on 8883, was picked up by the connection watchdog exactly as intended;
the rebuild ended in that join and the process stopped serving HTTP while
staying alive). The add-printer probe hit the same join as #1445 and tears its
client down off-loop instead; everything else goes through here.
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)

# How long a retirement may take before it is worth a WARNING. A healthy paho
# network thread exits in well under a second.
_RETIRE_SLOW_SECONDS = 5.0

# The callbacks BamDude's three paho users (printers, relay, smart plugs) set
# between them. Everything else paho offers stays None because nobody assigns it.
_CALLBACKS = ("on_connect", "on_disconnect", "on_subscribe", "on_message")


def retire_paho_client(client, label: str) -> threading.Thread | None:
    """Shut *client* down on a thread of its own and return immediately.

    *label* names the connection in the log and in the thread's name, which is
    where a thread dump from the next stuck one will be read.

    ⚠️ The callbacks are detached HERE, before returning -- not on the thread.
    Blocking until the network thread was gone is what used to guarantee that a
    client we let go of could no longer touch our state; with the teardown
    detached, a zombie that finishes its handshake would auto-reconnect and
    report itself connected behind its replacement's back. paho copies a
    callback under ``_callback_mutex`` and invokes it outside it, so the setter
    never waits on a callback in flight.

    The DISCONNECT still goes out, first thing on the thread: it is what stops
    paho's auto-reconnect, and with it an unacked ``project_file`` replaying
    onto a revived session (#1136).

    Returns the teardown thread (tests join it), or None when no thread could be
    started -- the DISCONNECT is then sent inline and the network thread is left
    to paho.
    """
    for attr in _CALLBACKS:
        try:
            setattr(client, attr, None)
        except Exception:  # noqa: BLE001 — paho always allows this; a stub may not
            pass

    def _teardown() -> None:
        started = time.monotonic()
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001 — the old client is often already broken
            logger.debug("[%s] Retiring MQTT client: disconnect raised", label, exc_info=True)
        try:
            client.loop_stop()
        except Exception:  # noqa: BLE001
            logger.debug("[%s] Retiring MQTT client: loop_stop raised", label, exc_info=True)
        waited = time.monotonic() - started
        if waited >= _RETIRE_SLOW_SECONDS:
            # This used to be the event loop's stall. It means the connection is
            # wedged somewhere paho cannot interrupt -- say so, so the next one
            # is not diagnosed from a thread dump.
            logger.warning(
                "[%s] Retiring the old MQTT client took %.0fs (paho's network thread would not stop); "
                "it was let go without waiting",
                label,
                waited,
            )

    try:
        thread = threading.Thread(target=_teardown, name=f"mqtt-retire-{label}", daemon=True)
        thread.start()
        return thread
    except RuntimeError as exc:
        # Out of threads: the process has larger problems. The DISCONNECT is
        # still sent inline -- it is what keeps the abandoned session from
        # reconnecting and replaying (#1136).
        logger.error("[%s] Could not start the MQTT teardown thread: %s", label, exc)
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        return None
