"""The one failure-reason vocabulary (upstream 5211fd45, #2974).

``PrintArchive.failure_reason`` holds a KEY, rendered through
``t('editArchive.failureReasons.<key>')`` by the archive editor and the
Statistics breakdown — never a display label or a sentence. The Failure Analysis
widget groups on the raw value, so a second spelling of one cause is a second
bucket, and a label can never be translated because there is no key to resolve.
The list mirrors ``FAILURE_REASON_KEYS`` in ``frontend/src/components/EditArchiveModal.tsx``;
a test pins the two together. m186 folds the historical spellings onto these.
"""

from __future__ import annotations

from backend.app.i18n import current_language, t

FAILURE_REASON_KEYS = frozenset(
    {
        "adhesionFailure",
        "spaghettiDetached",
        "layerShift",
        "cloggedNozzle",
        "filamentRunout",
        "warping",
        "stringing",
        "underExtrusion",
        "powerFailure",
        "swapModeFailure",
        "printerError",
        "userCancelled",
        # No end-of-print status ever arrived (stale cleanup, reconnect
        # reconciliation); which situation it was is carried by ``status``.
        "noStatusUpdate",
        "other",
    }
)

USER_CANCELLED = "userCancelled"
NO_STATUS_UPDATE = "noStatusUpdate"


def reason_label(value: str | None, lang: str | None = None) -> str:
    """The words for a stored reason, in ``lang`` (the system language by default).

    A key reads as its label (``data/failure_reasons_<lang>.json``, the frontend's
    ``editArchive.failureReasons``); anything else — free text an older build or a
    user left — is shown as it is.
    """
    if not value:
        return ""
    if value not in FAILURE_REASON_KEYS:
        return value
    return t(lang or current_language(), "failure_reasons", value)
