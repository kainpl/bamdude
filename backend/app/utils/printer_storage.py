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


def _model_allows_printing_without_a_card(model: str | None) -> bool:
    """What the mirrored config says about this model.

    ⚠️ **In BambuStudio this flag only ever REFUSES.** All five of its uses read
    ``if (!SupportPrintWithoutSD() && sdcard_state == NO_SDCARD) → refuse``; it
    never grants anything. Permission to send a print with no card comes from
    the live ``fun2`` bit 0, which Studio defaults to ``false`` and sets from
    nowhere else. Using this flag as a grant makes us more permissive than
    Studio on any firmware that never reports that bit.

    ⚠️ The flag lives under the ``print`` block, mirroring BS's
    ``ParseVal(print_json, "support_print_without_sd", …)`` — off the config
    root it answers ``None`` for every model, i.e. "no machine can".

    Measured across all fifteen mirrored configs: true for X2D, P2S, H2C, H2D,
    H2D Pro, H2S, X1, X1 Carbon and X1E; false for P1P, P1S, A1 mini, A1 and
    A2L. (``support_save_remote_print_file_to_storage`` tracks it everywhere
    except X1E, where it is absent — so the two are close but not the same
    question, and only this one is used here.)
    """
    from backend.app.utils.printer_configs import load_printer_config

    config = load_printer_config(model) or {}
    return bool((config.get("print") or {}).get("support_print_without_sd"))


def _may_browse_internal(sup: dict, model: str | None) -> bool:
    """Live bit 17 if it has arrived, otherwise what the model is known to be.

    ⚠️ Deliberately the permissive side of the two, and only because the cost of
    being wrong is a listing that comes back empty and a switch back. The
    fallback is not a nicety: ``print_option_support`` is rebuilt from scratch
    on every reconnect and the printer sends its support block once, so treating
    an absent bit as ``False`` made the storage switcher vanish from the file
    browser after every reconnect.
    """
    reported = sup.get("model_internal_storage")
    if isinstance(reported, bool):
        return reported
    return _model_allows_printing_without_a_card(model)


def _may_print_without_a_card(sup: dict, model: str | None) -> bool:
    """Both conditions, exactly as BambuStudio applies them.

    ⚠️ **No fallback to the config here, unlike browsing.** The live bit is the
    grant and its absence is a refusal — Studio's own default. A printer that
    has not yet reported ``fun2``, or whose firmware never does, is refused
    rather than sent to a medium nobody has confirmed exists. Being generous
    with a listing costs an empty screen; being generous here costs an upload to
    a storage that may not be there and a dispatch that dies after it.
    """
    if not _model_allows_printing_without_a_card(model):
        return False
    return sup.get("print_with_emmc") is True


def storage_capability_for(model: str | None, state: object) -> dict:
    """Everything the browser and the dispatcher need, answered once."""
    sup = getattr(state, "print_option_support", None) or {}
    card_state = int(getattr(state, "sdcard_state", SDCARD_NONE) or 0)
    can_browse_internal = _may_browse_internal(sup, model)

    # ⚠️ "Nothing reported" is not "no card". With no live state at all,
    # ``card_state`` is 0 because the printer has never spoken, and reading that
    # as an empty slot would route a print into internal storage on a machine
    # holding a card. Both answers below depend on this, and they must agree:
    # a ``default_storage`` of external beside a ``print_target`` of internal
    # would be two answers to one question.
    card_state_is_known = state is not None

    if not card_state_is_known or card_state == SDCARD_NORMAL:
        print_target, reason = EXTERNAL, None
    elif card_state in (SDCARD_ABNORMAL, SDCARD_READONLY):
        print_target, reason = None, REASON_CARD_UNUSABLE
    elif _may_print_without_a_card(sup, model):
        print_target, reason = INTERNAL, None
    else:
        print_target, reason = None, REASON_NO_CARD_NO_INTERNAL

    storages = [EXTERNAL, INTERNAL] if can_browse_internal else [EXTERNAL]

    # ⚠️ Deliberate divergence from BambuStudio, which stays on the external tab
    # and paints an empty grid. An empty screen where files exist reads as a
    # malfunction, so a missing card opens the storage that has something in it.
    #
    # ⚠️ But only on evidence — see ``card_state_is_known`` above. Unknown falls
    # back to the medium every model has.
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
