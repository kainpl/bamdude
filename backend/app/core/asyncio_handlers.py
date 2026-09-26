"""Event-loop concerns handled at app startup.

Two of them, both about the loop BamDude finds itself on:
``install_proactor_reset_filter`` silences the noisy Windows
``_ProactorBasePipeTransport._call_connection_lost`` ``WinError 10054`` that
fires every time a printer / MQTT broker / camera RSTs a TCP socket instead of
closing it cleanly; ``warn_if_running_on_uvloop`` says so out loud when the loop
is uvloop, which BamDude is not launched on and does not want.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)


def _is_proactor_connection_reset(context: dict[str, Any]) -> bool:
    """True if ``context`` describes the Windows Proactor cleanup-RST noise.

    asyncio's default exception handler is invoked in two distinct cases
    we care about — generic uncaught task exceptions, and the specific
    ``_call_connection_lost`` cleanup path — and we only want to suppress
    the latter. Match on three signals together so a real
    ``ConnectionResetError`` raised inside an application task still
    surfaces normally:

      1. The exception is ``ConnectionResetError`` (or a subclass).
      2. asyncio's own message string mentions ``_call_connection_lost``
         (the Proactor-cleanup callback is the only place Python emits
         this exact phrase).
      3. We're actually on Windows, where the Proactor is in use.
    """
    if sys.platform != "win32":
        return False
    exc = context.get("exception")
    if not isinstance(exc, ConnectionResetError):
        return False
    message = context.get("message", "")
    return "_call_connection_lost" in message


def _proactor_reset_filter(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """Custom event-loop exception handler.

    Handles the Proactor-cleanup ``ConnectionResetError`` by logging it
    at DEBUG instead of ERROR, and delegates everything else to
    asyncio's default handler so unrelated bugs are still visible.
    """
    if _is_proactor_connection_reset(context):
        logger.debug(
            "asyncio Proactor: peer reset socket during cleanup (WinError 10054); "
            "ignored — application-layer reconnect handles the disconnect"
        )
        return
    loop.default_exception_handler(context)


def install_proactor_reset_filter(loop: asyncio.AbstractEventLoop | None = None) -> bool:
    """Install the filter on ``loop`` (or the running loop if omitted).

    Returns True when the filter was installed (Windows only), False on
    every other platform — so callers can branch on the return value if
    they want to log the install / skip.
    """
    if sys.platform != "win32":
        return False
    if loop is None:
        loop = asyncio.get_running_loop()
    loop.set_exception_handler(_proactor_reset_filter)
    return True


# Every launch path BamDude ships pins it: the Dockerfile, install/install.sh
# (systemd + launchd), deploy/bamdude.service and the Windows service.
_LOOP_FLAG = "--loop asyncio"


def running_on_uvloop(loop: object | None = None) -> bool:
    """Is *loop* (or the running loop) a uvloop loop?

    Asks the loop what it is, not whether uvloop imports: ``uvicorn[standard]``
    installs uvloop on every Linux venv, so its presence says nothing. Matching
    on the module name keeps this from importing uvloop just to ask — an
    ImportError on a host without it.
    """
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
    return type(loop).__module__.split(".")[0] == "uvloop"


def warn_if_running_on_uvloop(loop: object | None = None) -> bool:
    """One loud WARNING when the process runs on uvloop (upstream 0dfcff59). True when emitted.

    uvloop's TLS layer can drop buffered data when a client closes without a TLS
    close_notify, so a Virtual Printer FTP upload can be truncated, acknowledged
    and forwarded as a corrupt 3MF (#1896). ``virtual_printer/ftp_server.py``
    ZIP-validates a ``.3mf`` before acknowledging it, but that is a backstop,
    not a licence to run the loop that needs it. A warning, not a refusal:
    uvicorn chose its loop before any of this runs, and a server that answers
    beats one that will not boot. Who it reaches: a unit written by a
    third-party script with no loop pinned, and a native install created before
    install.sh gained the flag (2026-07-08) whose unit ``update.sh`` could not
    repair.
    """
    if not running_on_uvloop(loop):
        return False
    logger.warning(
        "Running on uvloop, which BamDude is not tested or shipped on: Virtual Printer FTP uploads can be "
        "silently truncated on this loop. Add '%s' to the uvicorn command in your service file and restart. "
        "Every installer BamDude ships already does this, and install/update.sh repairs a plain unit written "
        "before it did; a unit from a third-party script is not touched.",
        _LOOP_FLAG,
    )
    return True
