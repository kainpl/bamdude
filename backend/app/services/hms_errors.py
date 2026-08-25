"""Why a print is paused.

⚠️ This module used to also carry ``HMS_ERROR_DESCRIPTIONS`` — 853 entries
lifted from ha-bambulab, keyed by short code and the same for every model. It
disagreed with Bambu's own catalogue in 159 places, and not cosmetically:
``0300_401F`` was "The hotend is not installed" where Bambu's X2D text says
"The **right** hotend is not installed". On a two-nozzle machine that is a
different fault. Descriptions now come from ``hms_catalogue``, per model.
"""

from dataclasses import dataclass

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


# ---------------------------------------------------------------------------
# Runout classification (spec: docs/superpowers/specs/2026-08-23-filament-
# usage-accuracy-design.md §2). ⚠️ FULL ecodes only — the short ``MMMM_EEEE``
# form collapses distinct errors: ``12FF2000_00020001`` (holder ran out) and
# ``12FF8000_00020001`` (tangled/stuck) both shorten to ``12FF_0001``, observed
# live on an A1 mini 2026-08-23. A jam must never classify as a ZEROING
# runout (it gets at most the ``ambiguous`` kind — timeline marker only), and
# a spool whose slot was guessed is never zeroed — so unknown codes return
# None and ambiguity is an explicit kind, not a fallthrough.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunoutMatch:
    kind: str  # pause | autoswitch | external | ambiguous
    scope: str  # ams_slot | ams_unit | external | generic
    slot_in_unit: int | None  # 0-3 for ams_slot scope
    external_tray: int | None  # 254 / 255 for external scope
    transitional: bool  # purge-phase codes fold into the slot's existing event


# 16-char hms[] family, device 0700 (AMS). Mid bytes 20/21/22/23 name the slot
# inside the reporting unit; the unit itself is the detector's question.
_AMS_SLOT_RUNOUT_SUFFIXES: dict[str, tuple[str, bool]] = {
    "00020001": ("pause", False),  # ran out, waiting for a new filament
    "00030002": ("autoswitch", False),  # ran out, switched to the backup slot
    "00030001": ("pause", True),  # ran out, purging the old filament
    "00020005": ("pause", True),  # ran out, purge went abnormal
}

# 8-char print_error path: device byte + unit byte + error half.
_EXTERNAL_ERROR_HALVES = ("8011", "8030", "C030")


def classify_runout_ecode(full_code: str) -> RunoutMatch | None:
    """Classify a FULL ecode as a runout signal, or ``None`` for everything else.

    16-char codes come from the ``hms[]`` path (``HMSError.full_code``), 8-char
    ones from ``print_error``. Kinds: ``pause`` (printer waits for filament),
    ``autoswitch`` (AMS backup took over, no pause), ``external`` (spool holder
    / external feed), ``ambiguous`` (a unit-less generic code — journal
    timeline only, never a zero correction).
    """
    code = (full_code or "").upper()

    if len(code) == 16:
        if code.startswith("0700") and code[8:] in _AMS_SLOT_RUNOUT_SUFFIXES:
            kind, transitional = _AMS_SLOT_RUNOUT_SUFFIXES[code[8:]]
            if code[4:6] in ("20", "21", "22", "23"):
                return RunoutMatch(kind, "ams_slot", int(code[4:6], 16) - 0x20, None, transitional)
            if code[4:6] == "70":
                # Unit-scoped phrasing ("put a new filament into the same slot").
                return RunoutMatch(kind, "ams_unit", None, None, True)
            return None
        if code.startswith("070070") and code[8:] == "00020007":
            # Unit-scoped generic ("put a new filament into the same slot in
            # AMS") — the paired non-transitional per-slot code carries the
            # boundary, this one only confirms it.
            return RunoutMatch("pause", "ams_unit", None, None, True)
        if code.startswith("12FF20") and code[8:] in ("00020001", "00020002"):
            # A1-family spool holder: ran out / empty. 12FF80xx is the jam half.
            return RunoutMatch("external", "external", None, 254, False)
        if code.startswith("12FF80") and code[8:] == "00020001":
            # A1-family holder jam ("may be tangled or stuck") — the other half
            # of the reused 12FF_0001 short form. A reel's taped tail presents
            # as a jam, and the human may answer it with a fresh spool (measured
            # live 2026-08-25: replaced + reassigned mid-pause, and the journal
            # had nothing to attach the assignment to). Ambiguous puts the
            # timeline marker in so that assignment becomes a spool_loaded
            # boundary — never a zero correction, and untangle-and-resume with
            # the same reel journals no boundary at all.
            return RunoutMatch("ambiguous", "external", None, 254, False)
        return None

    if len(code) == 8:
        device, unit, half = code[:2], code[2:4], code[4:]
        if code == "03008015":
            return RunoutMatch("external", "external", None, 254, False)
        if code == "03008004":
            return RunoutMatch("ambiguous", "generic", None, None, False)
        if device in ("07", "12", "18"):
            if unit == "FE" and half in _EXTERNAL_ERROR_HALVES:
                return RunoutMatch("external", "external", None, 254, False)
            if unit == "FF" and half in _EXTERNAL_ERROR_HALVES:
                # H2-series FF is the right/aux external; on the A1 family the
                # holder is the only external feed and reports as 254.
                tray = 254 if device == "12" else 255
                return RunoutMatch("external", "external", None, tray, False)
            if half == "8011" and unit not in ("FE", "FF"):
                return RunoutMatch("pause", "ams_unit", None, None, False)
        return None

    return None
