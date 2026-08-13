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


def _model_has_internal_storage(model: str | None) -> bool:
    """What the mirrored config says, for use when the live bits are absent.

    ⚠️ The flag lives under the ``print`` block, mirroring BS's
    ``ParseVal(print_json, "support_print_without_sd", …)`` — reading it off the
    config root silently answers ``None`` for every model, which reads as "no
    machine has internal storage".

    ⚠️ **One config flag stands in for two different live bits.** Measured
    across all ten mirrored configs, ``support_print_without_sd`` and
    ``support_save_remote_print_file_to_storage`` agree exactly: both true for
    X2D / H2D / H2S / P2S / X1 / X1C, both absent for every P1 and A1. The two
    live bits they stand in for are genuinely separate questions, but no
    per-model data distinguishes them, and inventing a distinction the data
    does not carry would be worse than admitting the fallback is coarse.
    """
    from backend.app.utils.printer_configs import load_printer_config

    config = load_printer_config(model) or {}
    return bool((config.get("print") or {}).get("support_print_without_sd"))


def _reported_or_model(sup: dict, key: str, model: str | None) -> bool:
    """Live report wins; the mirrored model config answers before the first push.

    Same precedence as ``timelapse.capability_for``, and it is not a nicety:
    ``print_option_support`` is rebuilt from scratch on every reconnect, and
    Bambu sends the full support block once and then sparse deltas. Treating an
    absent bit as ``False`` made the storage switcher vanish from the file
    browser every time a printer reconnected — for as long as it took the next
    full push to arrive.
    """
    reported = sup.get(key)
    if isinstance(reported, bool):
        return reported
    return _model_has_internal_storage(model)


def storage_capability_for(model: str | None, state: object) -> dict:
    """Everything the browser and the dispatcher need, answered once."""
    sup = getattr(state, "print_option_support", None) or {}
    card_state = int(getattr(state, "sdcard_state", SDCARD_NONE) or 0)
    can_browse_internal = _reported_or_model(sup, "model_internal_storage", model)

    if card_state == SDCARD_NORMAL:
        print_target, reason = EXTERNAL, None
    elif card_state in (SDCARD_ABNORMAL, SDCARD_READONLY):
        print_target, reason = None, REASON_CARD_UNUSABLE
    elif _reported_or_model(sup, "print_with_emmc", model):
        print_target, reason = INTERNAL, None
    else:
        print_target, reason = None, REASON_NO_CARD_NO_INTERNAL

    storages = [EXTERNAL, INTERNAL] if can_browse_internal else [EXTERNAL]

    # ⚠️ Deliberate divergence from BambuStudio, which stays on the external tab
    # and paints an empty grid. An empty screen where files exist reads as a
    # malfunction, so a missing card opens the storage that has something in it.
    #
    # ⚠️ But only on evidence. With no live state at all — a printer that has
    # never connected, or is offline — ``card_state`` is 0 because nothing was
    # reported, not because the slot is empty. Reading that absence as "no card"
    # would open internal storage on a machine that has a card sitting in it.
    # Unknown falls back to the medium every model has.
    card_state_is_known = state is not None
    default_storage = (
        INTERNAL if (can_browse_internal and card_state_is_known and card_state != SDCARD_NORMAL) else EXTERNAL
    )

    return {
        "storages": storages,
        "can_browse_internal": can_browse_internal,
        "card_state": card_state,
        "default_storage": default_storage,
        "print_target": print_target,
        "reason": reason,
    }
