"""VP-local ownership. A caller timeout never cancels accepted local work."""

import asyncio
from contextlib import contextmanager

DRAIN_TIMEOUT = 30.0
TLS_CLOSE_GRACE = 1.0
TLS_HANDSHAKE_TIMEOUT = 5.0
VP_DRAIN_MESSAGE = (
    "Virtual printer work has not stopped. Wait for accepted uploads to finish, "
    "then retry in this process. Restart after restoring, or to abandon this attempt."
)


class VPDrainRefused(RuntimeError):
    def __init__(self, reason="pending"):
        super().__init__(VP_DRAIN_MESSAGE)
        self.reason = reason


class AdmissionClosed(RuntimeError):
    pass


def restore_required() -> bool:
    from backend.app.services.farm_startup_guard import farm_startup_guard

    return farm_startup_guard.restart_required


def drain_warning(outcome, *, git=False):
    from backend.app.i18n.api_errors import current_language, translate_sentence

    lang = current_language()
    messages = []
    cuts = sum(outcome["proxy_connections_cut"].values())
    leases = outcome["camera_leases_released"]
    missing = outcome["uploads_without_options"]
    if cuts:
        messages.append(
            translate_sentence("Virtual printer connections interrupted: {count}.", lang).format(count=cuts)
        )
    if leases:
        messages.append(translate_sentence("Camera proxy leases released: {count}.", lang).format(count=leases))
    if missing:
        messages.append(
            translate_sentence("Accepted uploads without slicer options: {count}.", lang).format(count=missing)
        )
        if git:
            messages.append(
                translate_sentence(
                    "Inspect pending jobs and their options before restarting and resuming. Resending can duplicate a job.",
                    lang,
                )
            )
    return " ".join(messages)


async def wait_owned(task, timeout=DRAIN_TIMEOUT):
    """Observe, never cancel, an already-published owner (including on request cancel)."""
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        raise VPDrainRefused()
    if task.cancelled():
        raise VPDrainRefused("owner_cancelled")
    try:
        return task.result()
    except VPDrainRefused:
        raise
    except Exception as exc:
        raise VPDrainRefused("cleanup_failed") from exc


async def close_writer(writer):
    """An idle TLS peer gets a short grace, then transport abort, then real join."""
    writer.close()
    task = asyncio.create_task(writer.wait_closed())
    try:
        done, _ = await asyncio.wait({task}, timeout=TLS_CLOSE_GRACE)
        if not done:
            writer.transport.abort()
        await task
    except (OSError, ConnectionError):
        # Failed TLS close is terminal once the transport has closed.
        pass
    finally:
        # A cancellation must not leave a close operation detached.
        if not task.done():
            writer.transport.abort()
            await asyncio.shield(task)


class Activities:
    def __init__(self):
        self.closing = False
        self.failed = False
        self._business = 0
        self._quiet = asyncio.Event()
        self._quiet.set()

    @property
    def closed(self):
        return self.closing or restore_required()

    @contextmanager
    def business(self):
        if self.closed:
            raise AdmissionClosed()
        self._business += 1
        self._quiet.clear()
        try:
            yield
        except BaseException:
            self.failed = True
            raise
        finally:
            self._business -= 1
            if not self._business:
                self._quiet.set()

    async def join(self):
        await self._quiet.wait()
        if self.failed:
            raise VPDrainRefused("business_failed")


class Connections:
    """Handlers registered before their first await, including pre-CONNECT/connect."""

    def __init__(self):
        self.handlers = {}
        self.cuts = 0

    @contextmanager
    def track(self, writer):
        task = asyncio.current_task()
        self.handlers[task] = writer
        try:
            yield
        finally:
            self.handlers.pop(task, None)

    async def disconnect(self):
        snapshot = dict(self.handlers)
        self.cuts += sum(not writer.is_closing() for writer in snapshot.values())
        await asyncio.gather(*(close_writer(w) for w in snapshot.values()))
        tasks = [t for t in snapshot if t is not asyncio.current_task()]
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                raise result
