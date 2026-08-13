"""Which storages a printer has, and where a print is allowed to go.

Composed here rather than in the routes so the browser's storage switcher, the
dispatcher's transport choice and the text of the refusal cannot disagree — the
same reason :func:`backend.app.utils.timelapse.capability_for` exists.

⚠️ **Two different fun2 bits, two different questions.** Bit 0
(``is_support_print_with_emmc``) decides whether a print may be sent with no
card; bit 17 (``is_support_model_internal_storage``) decides whether the file
browser offers an internal tab. BambuStudio keeps them apart
(``SelectMachine.cpp`` vs ``MediaFilePanel.cpp``) and so do we: a machine can
show you its eMMC and still refuse to print from it.

⚠️ **A damaged card is a hard stop, even on a machine with eMMC.** Studio's
gate reads ``if (NO_SDCARD && !emmc) refuse; else if (ABNORMAL || READONLY)
refuse;`` — the internal escape hatch covers a MISSING card only. Do not
"improve" this into a fallback: a card the printer cannot read means something
is wrong with the machine, and quietly routing around it hides the fault. This
is the same trap as the substring test on ``"HAS_SDCARD"`` that once let a
firmware upload target an unusable card.
"""

from __future__ import annotations

from backend.app.utils.timelapse import (
    SDCARD_ABNORMAL,
    SDCARD_NONE,
    SDCARD_NORMAL,
    SDCARD_READONLY,
)

EXTERNAL = "external"
INTERNAL = "internal"

# i18n keys, not sentences — the reason is rendered in the browser and the
# backend has no business choosing the user's language.
REASON_NO_CARD_NO_INTERNAL = "no_card_no_internal"
REASON_CARD_UNUSABLE = "card_unusable"


def _can_print_without_card(model: str | None, sup: dict) -> bool:
    """Live report wins; the mirrored model config answers before the first push.

    Same precedence as ``timelapse.capability_for``: a printer that has not
    pushed ``fun2`` yet would otherwise be treated as unable, which is both
    wrong for X2D and needlessly destructive.

    ⚠️ The config flag lives under the ``print`` block, mirroring BS's
    ``ParseVal(print_json, "support_print_without_sd", …)`` — reading it off the
    config root silently answers ``None`` for every model, which reads as "no
    machine can print without a card".
    """
    reported = sup.get("print_with_emmc")
    if isinstance(reported, bool):
        return reported

    from backend.app.utils.printer_configs import load_printer_config

    config = load_printer_config(model) or {}
    return bool((config.get("print") or {}).get("support_print_without_sd"))


def storage_capability_for(model: str | None, state: object) -> dict:
    """Everything the browser and the dispatcher need, answered once."""
    sup = getattr(state, "print_option_support", None) or {}
    card_state = int(getattr(state, "sdcard_state", SDCARD_NONE) or 0)
    can_browse_internal = bool(sup.get("model_internal_storage"))

    if card_state == SDCARD_NORMAL:
        print_target, reason = EXTERNAL, None
    elif card_state in (SDCARD_ABNORMAL, SDCARD_READONLY):
        print_target, reason = None, REASON_CARD_UNUSABLE
    elif _can_print_without_card(model, sup):
        print_target, reason = INTERNAL, None
    else:
        print_target, reason = None, REASON_NO_CARD_NO_INTERNAL

    storages = [EXTERNAL, INTERNAL] if can_browse_internal else [EXTERNAL]

    # ⚠️ Deliberate divergence from BambuStudio, which stays on the external tab
    # and paints an empty grid. An empty screen where files exist reads as a
    # malfunction, so a missing card opens the storage that has something in it.
    default_storage = INTERNAL if (can_browse_internal and card_state != SDCARD_NORMAL) else EXTERNAL

    return {
        "storages": storages,
        "can_browse_internal": can_browse_internal,
        "card_state": card_state,
        "default_storage": default_storage,
        "print_target": print_target,
        "reason": reason,
    }
