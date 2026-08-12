"""Whether a timelapse can be recorded at all, and whether there is room for it.

Two different questions, and BambuStudio asks them at two different moments:

* :func:`can_enable_timelapse` mirrors ``MachineObject::canEnableTimelapse`` and
  decides whether the checkbox may be ticked. Studio disables the box and puts
  the reason in its tooltip;
* :func:`is_storage_low` mirrors ``DevStorage::is_timelapse_storage_low`` and
  runs just before the print starts, on machines with internal storage. Studio
  warns and offers to delete the oldest recording.

⚠️ **The four SD-card states are not two.** ``canEnableTimelapse`` gives a
different reason for a missing card, an unreadable one and a read-only one, and
only ``HAS_SDCARD_NORMAL`` passes. Collapsing them to "has a card / has none"
loses the two cases where a card is present and still unusable — exactly the
pair a substring test on the raw string got wrong here once before.

⚠️ **An SD card is not required when the machine has somewhere else to put it.**
Internal timelapse storage short-circuits the whole check, and a timelapse kit
excuses a card that is missing or unhappy. Neither is inferable from the model
name: one is ``fun`` bit 28, the other ``aux`` bit 26.
"""

from __future__ import annotations

# BS ``DevStorage::SdcardState``. Only NORMAL is usable.
SDCARD_NONE = 0
SDCARD_NORMAL = 1
SDCARD_ABNORMAL = 2
SDCARD_READONLY = 3

# ⚠️ BS: ``const int THRESHOLD_KB = 20480; // 10MB``. The comment and the number
# disagree — 20480 KB is 20 MB — and the NUMBER is what runs, so it is what we
# mirror. Reading the comment instead would warn at half the space Studio does.
STORAGE_LOW_THRESHOLD_KB = 20480

# i18n keys rather than sentences: the reason is shown in the browser, and the
# backend has no business choosing the user's language.
REASON_UNSUPPORTED = "unsupported"
REASON_NO_STORAGE = "no_storage"
REASON_STORAGE_UNAVAILABLE = "storage_unavailable"
REASON_STORAGE_READONLY = "storage_readonly"

_SDCARD_REASON = {
    SDCARD_NONE: REASON_NO_STORAGE,
    SDCARD_ABNORMAL: REASON_STORAGE_UNAVAILABLE,
    SDCARD_READONLY: REASON_STORAGE_READONLY,
}


def can_enable_timelapse(
    *,
    supports_timelapse: bool,
    supports_internal_timelapse: bool,
    has_timelapse_kit: bool,
    sdcard_state: int,
) -> tuple[bool, str | None]:
    """BS ``canEnableTimelapse``, reason included.

    Returns ``(True, None)`` or ``(False, reason_key)``. The order of the tests
    is Studio's and matters: internal storage answers the question before the
    card is ever consulted.
    """
    if not supports_timelapse:
        return False, REASON_UNSUPPORTED

    if supports_internal_timelapse:
        return True, None

    if sdcard_state != SDCARD_NORMAL and not has_timelapse_kit:
        # ⚠️ A state outside the four is not a licence to proceed — an unknown
        # card state is not a working card.
        return False, _SDCARD_REASON.get(sdcard_state, REASON_NO_STORAGE)

    return True, None


def default_storage(*, supports_internal_timelapse: bool, sdcard_state: int) -> str:
    """Which target a timelapse would be written to.

    BS defaults to ``"internal"`` where the machine has it, and falls back to it
    whenever there is no card to hold the external one
    (``if (!has_sdcard && m_timelapse_storage == "external")``).
    """
    if supports_internal_timelapse or sdcard_state != SDCARD_NORMAL:
        return "internal"
    return "external"


def is_storage_low(storage_info: dict, storage: str) -> bool:
    """Whether the chosen target is nearly full.

    ⚠️ Answers False for a target the camera never reported. BS guards on
    ``free_kb >= 0`` with the field initialised to -1, so "unknown" is not
    "empty" — warning about space nobody measured would train people to click
    through the warning that matters.
    """
    key = "tl_internal_free_kb" if storage == "internal" else "tl_external_free_kb"
    free = storage_info.get(key)
    if not isinstance(free, int) or isinstance(free, bool) or free < 0:
        return False
    return free < STORAGE_LOW_THRESHOLD_KB


def capability_for(model: str | None, state: object) -> dict:
    """Everything the UI needs to draw the timelapse option, answered once.

    Composed here rather than in the projections so the checkbox, the pre-print
    warning and the dispatcher's own refusal cannot disagree — the same reason
    ``temperature_limits.limits_for`` exists.
    """
    from backend.app.utils.printer_configs import supports_timelapse as model_supports_timelapse

    sup = getattr(state, "print_option_support", None) or {}
    sdcard_state = int(getattr(state, "sdcard_state", SDCARD_NONE) or 0)
    internal = bool(sup.get("internal_timelapse"))
    # ⚠️ The live report wins, and the MODEL answers when it has not arrived.
    # BS defaults this to False and would refuse a timelapse in the seconds
    # before the first push; every shipped config says the model can do it, so
    # falling back to the config is both truer and non-destructive.
    reported = sup.get("timelapse")
    supported = (
        bool(reported)
        if isinstance(reported, bool)
        else model_supports_timelapse(model, getattr(state, "firmware_version", None))
    )

    ok, reason = can_enable_timelapse(
        supports_timelapse=supported,
        supports_internal_timelapse=internal,
        has_timelapse_kit=bool(getattr(state, "has_timelapse_kit", False)),
        sdcard_state=sdcard_state,
    )
    storage = default_storage(supports_internal_timelapse=internal, sdcard_state=sdcard_state)
    info = dict(getattr(state, "timelapse_storage", None) or {})
    return {
        "can_enable": ok,
        "reason": reason,
        "storage": storage,
        "storage_low": is_storage_low(info, storage),
        "supports_internal": internal,
        "free_kb": info.get("tl_internal_free_kb" if storage == "internal" else "tl_external_free_kb"),
    }
