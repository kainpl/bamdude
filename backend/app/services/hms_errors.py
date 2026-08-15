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

# Operator-facing copy for the normalised reason keys. Kept here (not in
# JSON) because notifications go through the existing template engine which
# only substitutes scalar variables — translation of the *human-readable*
# reason text is handled by the per-locale ``notification_templates_*.json``
# overrides if they want to redefine ``{reason}`` content. For now the same
# English label is reused across locales (the HMS table is English-only
# upstream); future i18n can override per-key.
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


def _describe(device: str, short_code: str) -> str | None:
    """Bambu's own text for a code, or ``None``.

    ⚠️ ``device`` may be empty — a pause can be classified before we know which
    machine it came from. Then there is no description and the caller falls back
    to its generic label, which is exactly what happened for every code before
    this catalogue existed.
    """
    from backend.app.services.hms_catalogue import describe

    return describe(device, None, short_code.replace("_", "")) if device else None


def classify_pause_reason(
    hms_codes: list[str] | None,
    expected_reason: str | None = None,
    device: str = "",
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

    Returns:
        ``(reason_code, reason_label, hms_code)`` — ``reason_code`` is the
        normalised key for routing/filtering; ``reason_label`` is the
        operator-facing string used in notification ``{reason}`` variable;
        ``hms_code`` is the matched HMS code or ``None``.
    """
    if expected_reason and expected_reason in PAUSE_REASON_LABELS:
        return expected_reason, PAUSE_REASON_LABELS[expected_reason], None

    if hms_codes:
        for code in hms_codes:
            normalised = code.upper()
            if normalised in PAUSE_REASON_CODES:
                key = PAUSE_REASON_CODES[normalised]
                # Prefer the precise HMS description over the generic label —
                # operators want to see "The door seems to be open, so
                # printing was paused." not just "Door / cover open".
                desc = _describe(device, normalised) or PAUSE_REASON_LABELS[key]
                return key, desc, normalised
        # Unknown HMS code — surface the first one so operators can search
        # for it instead of getting a useless "Unknown".
        first = hms_codes[0].upper()
        desc = _describe(device, first) or PAUSE_REASON_LABELS["hms_other"]
        return "hms_other", desc, first

    return "unknown", PAUSE_REASON_LABELS["unknown"], None
