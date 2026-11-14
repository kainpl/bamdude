"""Which browser views are attached to a camera's fan-out right now.

A camera has ONE upstream shared by every viewer, and its status (`active` / `stalled`) answers for that
upstream. When the server ends ONE viewer cleanly — its authority was retired, its token expired — while
somebody else still watches the same camera, the upstream stays healthy and the ended viewer's `<img>`
keeps its last frame for good: nothing fires in the browser. So a view names each stream it opens with a
random correlation id, and its status read asks about that id.

The id is NOT a secret and NOT authority. It is registered only by a request that already passed the
stream route's own gate, and read only by a caller that already passed the status route's own gate for the
same camera; all it can ever answer is "attached / not" for that camera. It is never logged.

Process memory only: a restart forgets every id together with every stream.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# Short and plain: what `crypto.getRandomValues` rendered as base64url gives, and nothing a log line,
# a header or a path could be made of.
_VIEWER_ID = re.compile(r"[A-Za-z0-9_-]{8,32}")

# Distinct ids kept per camera. A wall, a window and a few overlays are a handful; the bound only stops
# a caller that passed the gate from growing the registry without end.
MAX_VIEWERS_PER_CAMERA = 64

# fan-out key -> viewer id -> open streams under that id (a view that re-requests its own address can
# overlap itself for a moment, so this is a count, not a flag).
_attached: dict[str, dict[str, int]] = {}
# Cameras that turned an id away at the bound: for them "not found" is unknown, not "detached".
_overflowed: set[str] = set()


def parse_viewer_id(raw: object) -> str | None:
    """The id when it is well-formed, else ``None`` — a malformed id is ignored, never an error."""
    if not isinstance(raw, str) or not _VIEWER_ID.fullmatch(raw):
        return None
    return raw


def attach(key: str, raw: object) -> Callable[[], None]:
    """Register a viewer of ``key``; the returned callable removes it, once, whatever ended the viewer."""
    viewer_id = parse_viewer_id(raw)
    if viewer_id is None:
        return lambda: None
    viewers = _attached.setdefault(key, {})
    if viewer_id not in viewers and len(viewers) >= MAX_VIEWERS_PER_CAMERA:
        _overflowed.add(key)
        return lambda: None
    viewers[viewer_id] = viewers.get(viewer_id, 0) + 1
    released = False

    def release() -> None:
        nonlocal released
        if released:
            return
        released = True
        current = _attached.get(key)
        if current is None:
            return
        remaining = current.get(viewer_id, 0) - 1
        if remaining > 0:
            current[viewer_id] = remaining
            return
        current.pop(viewer_id, None)
        if not current:
            _attached.pop(key, None)
            _overflowed.discard(key)

    return release


def attached(key: str, raw: object) -> bool | None:
    """Whether THIS viewer is attached to ``key``; ``None`` when that cannot be said.

    Unknown for a malformed (or absent) id, and for an id that is not registered on a camera that had to
    turn ids away — a view treats unknown as "no answer", never as a dead stream.
    """
    viewer_id = parse_viewer_id(raw)
    if viewer_id is None:
        return None
    if viewer_id in _attached.get(key, {}):
        return True
    return None if key in _overflowed else False


def forget_all() -> None:
    """Drop every record (tests)."""
    _attached.clear()
    _overflowed.clear()
