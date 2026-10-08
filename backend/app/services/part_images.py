"""Which picture a product part shows (spec §10). E4 writes the reader and the writer of this module.

E3 needs only the change collector: every transition in part_renders ends in ``mark_changed`` with the
``(file_sha256, plate_index)`` keys whose pictures may have changed. E4 subscribes and turns them into
the part-image events of spec §12.3.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)

_listeners: list[Callable[[set[tuple[str, int]]], None]] = []


def subscribe(listener: Callable[[set[tuple[str, int]]], None]) -> Callable[[], None]:
    _listeners.append(listener)
    return lambda: _listeners.remove(listener)


def mark_changed(keys: Iterable[tuple[str, int]]) -> None:
    changed = set(keys)
    if not changed:
        return
    for listener in list(_listeners):
        try:
            listener(changed)
        except Exception:
            logger.exception("Part image change listener failed")  # a listener never undoes a committed write
