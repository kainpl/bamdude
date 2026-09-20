"""Helper for test_embedded_postgres_live — not a test, the test spawns it.

Runs on a console of its own: starts the bundled server through the app's own
lifecycle module, sends this console's Ctrl+C to every process attached to it
(what the operator's keypress in the uvicorn window does), reports whether the
server survived, then stops it the way the lifespan does. Settings come from the
environment the test sets (DATA_DIR, DATABASE_URL=embedded, EMBEDDED_PG_PORT).
"""

import asyncio
import ctypes
import time

# The parent may run with Ctrl+C ignored (harnesses do), and that attribute is
# inherited down the whole tree — restore normal processing BEFORE anything is
# spawned, or the server would inherit "ignored" and the probe would prove nothing.
ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)

from backend.app.services import embedded_postgres as ep  # noqa: E402


def main() -> None:
    asyncio.run(ep.start())
    try:
        ctypes.windll.kernel32.GenerateConsoleCtrlEvent(0, 0)  # CTRL_C_EVENT, this console
        try:
            time.sleep(4)
        except KeyboardInterrupt:
            print("PROBE_GOT_CTRL_C", flush=True)
        time.sleep(2)
        print("ALIVE_AFTER_CTRL_C", asyncio.run(ep.is_running()), flush=True)
    finally:
        asyncio.run(ep.stop())
    print("STOPPED_CLEANLY", not asyncio.run(ep.is_running()), flush=True)


if __name__ == "__main__":
    main()
