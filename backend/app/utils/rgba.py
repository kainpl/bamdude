"""The one ``RRGGBBFF`` normaliser.

An opaque 8-digit colour is what the AMS payload carries and what the firmware
compares when it decides whether two trays are interchangeable, so the same
predicate is asked in two very different places: ``schemas/printer`` REFUSES a
bad canonical colour on the wire, while
``services/ams_backup_compatibility.BackupCompatibilityPolicy.from_dict``
CORRECTS one read off a persisted row that may predate any validator. Written
twice, they drift — this module owns the question and each caller decides what
to do with the ``None``.

``utils/color_utils`` is deliberately not the home: it is comparison maths
(CIEDE2000 and RGB distance) that never normalises a colour string.
"""

from __future__ import annotations

import re

_OPAQUE_RGBA = re.compile(r"[0-9A-F]{6}FF")


def normalize_opaque_rgba(value: str | None) -> str | None:
    """``RRGGBBFF`` upper-cased, or None when the value is not one.

    Surrounding whitespace and exactly ONE leading ``#`` (what a colour input
    hands over) are removed; a second ``#`` is a typo rather than a colour, and
    a translucent value is refused because it would emulate a profile no spool
    can ever match.
    """
    text = str(value or "").strip()
    text = text[1:] if text.startswith("#") else text
    text = text.upper()
    return text if _OPAQUE_RGBA.fullmatch(text) else None
