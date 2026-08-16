"""Why a print is paused.

⚠️ This module used to also carry ``HMS_ERROR_DESCRIPTIONS`` — 853 entries
lifted from ha-bambulab, keyed by short code and the same for every model. It
disagreed with Bambu's own catalogue in 159 places, and not cosmetically:
``0300_401F`` was "The hotend is not installed" where Bambu's X2D text says
"The **right** hotend is not installed". On a two-nozzle machine that is a
different fault. Descriptions now come from ``hms_catalogue``, per model.
"""

PAUSE_REASON_CODES: dict[str, str] = {
    # Maps HMS code → normalised pause-reason key. The dispatch path uses the
    # key for routing/filtering (frontend can highlight "filament_runout"
    # uniformly regardless of which exact HMS variant fired); the
    # human-readable text comes from ``HMS_ERROR_DESCRIPTIONS`` so we don't
    # double-maintain copy.
    "0300_8001": "user",
    "0300_8004": "filament_runout",
    "0300_8015": "filament_runout",
    "07FE_8030": "filament_runout",
    "07FF_8030": "filament_runout",
    "0300_800F": "door_open",
    "0300_8042": "door_open",
    "0300_804B": "door_open",
    "0500_8089": "presence_check",
    "0300_8013": "file_pause_command",
    "0300_8002": "ai_first_layer_defect",
    "0300_8003": "ai_spaghetti",
    "0300_800A": "ai_spaghetti",
    "0300_8017": "foreign_object",
}

# Fallback copy for the normalised reason keys, used when the catalogue has no
# description for the code — which since the catalogue went to 14 models means,
# in practice, BamDude's OWN pauses (plate objects, Obico) that raise no HMS
# code at all.
#
# ⚠️ **This table is the last resort, not the source.** The strings live in
# ``data/pause_reasons_{en,uk}.json`` and are read through ``t()``; the dict
# below answers only when a key is missing there, because ``t()`` returns the
# KEY on a miss and "filament_runout" in front of an operator is worse than
# English prose.
PAUSE_REASON_LABELS: dict[str, str] = {
    "user": "Paused by user",
    "filament_runout": "Filament runout",
    "door_open": "Door / cover open",
    "presence_check": "Presence-check failed",
    "file_pause_command": "G-code pause command",
    "ai_first_layer_defect": "AI: first-layer defect",
    "ai_spaghetti": "AI: spaghetti / pile-up",
    "foreign_object": "Foreign object on heatbed",
    "plate_objects": "Objects detected on plate",
    "hms_other": "HMS error",
    "unknown": "Unknown",
}


def _label(key: str, lang: str) -> str:
    """The generic reason text for a normalised key, in ``lang``.

    Its own namespace rather than ``notification_templates`` on purpose: that
    file seeds a DB table of templates, and a pause reason is not a template.
    ``measurements`` is the precedent — a small file read only through ``t()``.
    """
    from backend.app.i18n import t

    text = t(lang, "pause_reasons", key)
    return PAUSE_REASON_LABELS.get(key, key) if text == key else text


def _describe(device: str, short_code: str, lang: str) -> str | None:
    """Bambu's own text for a code, or ``None``.

    ⚠️ ``device`` may be empty — a pause can be classified before we know which
    machine it came from. Then there is no description and the caller falls back
    to its generic label, which is exactly what happened for every code before
    this catalogue existed.

    ⚠️ ``lang`` is not optional on purpose. It used to be omitted here, and the
    default quietly made every pause reason English — in the app and in
    Telegram — while the same catalogue served the error dialog in Ukrainian
    correctly, because that caller did pass it. A parameter that defaults to a
    language is a parameter that gets forgotten.
    """
    from backend.app.services.hms_catalogue import describe

    return describe(device, None, short_code.replace("_", ""), lang) if device else None


def classify_pause_reason(
    hms_codes: list[str] | None,
    expected_reason: str | None = None,
    device: str = "",
    lang: str | None = None,
) -> tuple[str, str, str | None]:
    """Resolve a pause's normalised reason key + human-readable text.

    Args:
        hms_codes: HMS codes currently active on the printer, in the
            ``MMMM_EEEE`` short form this module is keyed by — i.e.
            ``[e.short_code for e in PrinterState.hms_errors]``. May be ``None``.
            ⚠️ Not ``HMSError.code``: that is ``0x8004`` and matches no key here.
        expected_reason: Reason hint planted by an internal pause-trigger
            (e.g. plate-detect setting ``"plate_objects"`` before issuing
            the pause command). Wins over HMS classification when set —
            internal pauses don't always raise an HMS code, and when they
            do the HMS code is generic ("paused by user").
        lang: Language for the HMS description. Defaults to the system
            language held in process memory — this runs on the sync MQTT
            push path, which cannot await a settings read.

    Returns:
        ``(reason_code, reason_label, hms_code)`` — ``reason_code`` is the
        normalised key for routing/filtering; ``reason_label`` is the
        operator-facing string used in notification ``{reason}`` variable;
        ``hms_code`` is the matched HMS code or ``None``.
    """
    if lang is None:
        from backend.app.i18n import current_language

        lang = current_language()

    if expected_reason and expected_reason in PAUSE_REASON_LABELS:
        return expected_reason, _label(expected_reason, lang), None

    if hms_codes:
        for code in hms_codes:
            normalised = code.upper()
            if normalised in PAUSE_REASON_CODES:
                key = PAUSE_REASON_CODES[normalised]
                # Prefer the precise HMS description over the generic label —
                # operators want to see "The door seems to be open, so
                # printing was paused." not just "Door / cover open".
                desc = _describe(device, normalised, lang) or _label(key, lang)
                return key, desc, normalised
        # Unknown HMS code — surface the first one so operators can search
        # for it instead of getting a useless "Unknown".
        first = hms_codes[0].upper()
        desc = _describe(device, first, lang) or _label("hms_other", lang)
        return "hms_other", desc, first

    return "unknown", _label("unknown", lang), None
