"""One listing per printer directory at a time, and an answer that says what it proves.

G10 unit A (spec r4 §5.2, M03). A refresh from the file browser while a listing
of the same directory is still running joins that listing instead of opening
another session; a caller whose own wait runs out gets ``slow`` with the last
complete listing it can show (marked stale), never an empty list and never a
second socket. Cancelling one subscriber never cancels the shared listing — it
is a task of its own, and its actual FTP work is owned by the file lane.

The key is the endpoint as the request reaches it: printer id and identity,
address, a fingerprint of the access code, storage, directory and catalogue —
so a printer re-added under the same id, a changed address or new credentials
never join an old listing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import OrderedDict
from dataclasses import dataclass, field

from backend.app.services.printer_files.base import RemoteFile

logger = logging.getLogger(__name__)

LISTING_COMPLETE = "complete"
LISTING_MISSING = "missing"
LISTING_UNREADABLE = "unreadable"
LISTING_UNAVAILABLE = "unavailable"
LISTING_SLOW = "slow"

#: How many last complete listings are kept for a slow answer.
_REMEMBER = 256


@dataclass(frozen=True)
class ListingAnswer:
    """What one listing request can honestly say.

    ``status``: ``complete`` (``files`` is the directory), ``missing`` (absent,
    proven by a complete parent listing), ``unreadable`` (the printer refused:
    missing OR forbidden), ``unavailable`` (no session, or the listing failed),
    ``slow`` (still running when this caller's wait ran out). ``stale`` means
    ``files`` is the last complete listing of the directory, shown while the
    current one is not available.
    """

    status: str
    files: list[RemoteFile] = field(default_factory=list)
    stale: bool = False
    reason: str | None = None


def _key(printer, storage: str, path: str, file_type: str) -> tuple:
    code = getattr(printer, "access_code", "") or ""
    fingerprint = hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]
    return (
        getattr(printer, "id", None),
        getattr(printer, "connector_identity", None),
        getattr(printer, "ip_address", None),
        fingerprint,
        storage,
        path,
        file_type,
    )


class ListingCoalescer:
    def __init__(self) -> None:
        self._inflight: dict[tuple, asyncio.Task] = {}
        self._last: OrderedDict[tuple, list[RemoteFile]] = OrderedDict()

    async def list(self, printer, storage: str, path: str, file_type: str, transport, *, wait: float) -> ListingAnswer:
        key = _key(printer, storage, path, file_type)
        task = self._inflight.get(key)
        if task is None or task.done():
            task = asyncio.get_running_loop().create_task(
                transport.list_directory(path, file_type=file_type), name="printer-file-listing"
            )
            self._inflight[key] = task
            task.add_done_callback(lambda finished, key=key: self._settle(key, finished))
        try:
            status, files, reason = await asyncio.wait_for(asyncio.shield(task), wait)
        except TimeoutError:
            previous = self._last.get(key)
            return ListingAnswer(LISTING_SLOW, list(previous or []), stale=previous is not None, reason="in_progress")
        except Exception as exc:  # noqa: BLE001 — a transport bug must read as "could not tell", not as a 500
            logger.warning("Printer file listing raised %s", type(exc).__name__)
            status, files, reason = LISTING_UNAVAILABLE, [], "error"
        if status == LISTING_COMPLETE:
            return ListingAnswer(LISTING_COMPLETE, list(files))
        previous = self._last.get(key) if status == LISTING_UNAVAILABLE else None
        return ListingAnswer(status, list(previous or []), stale=previous is not None, reason=reason)

    def _settle(self, key: tuple, task: asyncio.Task) -> None:
        if self._inflight.get(key) is task:
            del self._inflight[key]
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.info("Printer file listing failed: %s", type(error).__name__)
            return
        status, files, _reason = task.result()
        if status == LISTING_COMPLETE:
            self._last[key] = list(files)
            self._last.move_to_end(key)
            while len(self._last) > _REMEMBER:
                self._last.popitem(last=False)
        elif status in (LISTING_MISSING, LISTING_UNREADABLE):
            self._last.pop(key, None)


listing_coalescer = ListingCoalescer()
