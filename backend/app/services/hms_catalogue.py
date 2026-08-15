"""HMS error descriptions, per printer model.

Companion to ``hms_actions.py``: that one answers "what can the operator do about
this", this one answers "what is it". Both are keyed by the first three
characters of the serial number, so there is one rule to learn rather than two.

Data comes from BambuStudio via ``scripts/import_hms_catalogue.py`` — see
``backend/app/data/hms/README.md`` for the layout and why it is per model.

⚠️ **No cross-model fallback.** 879 codes carry different text on different
machines (``0C00020000010001`` is a horizontal laser on one and a
height-measuring laser on another), so answering from a model we happen to have
loaded would describe the wrong mechanism with full confidence. An unknown model
answers ``None``, the UI says the code is unrecognised, and that is true.

⚠️ **Language falls back to English, never to nothing.** BambuStudio ships no
Ukrainian at all, so a missing translation is the common case rather than the
edge one — and a description in the wrong language beats a blank where a fault
should be.

Files are loaded on first use and cached: seven models across two languages is
~11 MB, and a farm only ever touches the models it owns.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "hms"

# Keyed by (lang, device). A miss is cached as an empty dict on purpose — see
# ``_load``.
_CATALOGUES: dict[tuple[str, str], dict[str, str]] = {}


def _path_for(lang: str, device: str) -> Path:
    """English sits at the root; every other language in its own directory."""
    return _DATA_DIR / f"{device}.json" if lang == "en" else _DATA_DIR / lang / f"{device}.json"


def _load(lang: str, device: str) -> dict[str, str]:
    """One model's catalogue, read once.

    ⚠️ A miss is cached too. Files are ~700 KB and an unknown model — or a
    language we ship no translation for — would otherwise mean a filesystem
    probe for every error on every status push.
    """
    key = (lang, device)
    if key not in _CATALOGUES:
        try:
            _CATALOGUES[key] = json.loads(_path_for(lang, device).read_text(encoding="utf-8"))
        except FileNotFoundError:
            _CATALOGUES[key] = {}
        except (OSError, ValueError) as exc:
            # A corrupt or unreadable file is worth saying out loud once; an
            # absent one is ordinary and silent.
            logger.warning("HMS catalogue %s/%s unreadable: %s", lang, device, exc)
            _CATALOGUES[key] = {}
    return _CATALOGUES[key]


def device_of(printer_id: int) -> str:
    """The catalogue key for a connected printer — its serial's first three
    characters.

    Returns ``""`` for a printer we have no info for, which ``describe`` then
    answers ``None`` to. ⚠️ Never guess a model here: 879 codes describe
    different mechanisms on different machines, so the wrong model is worse than
    no description at all.
    """
    from backend.app.services.printer_manager import printer_manager

    info = printer_manager.get_printer(printer_id)
    return (getattr(info, "serial_number", "") or "")[:3].upper()


def shipped_devices(lang: str = "en") -> list[str]:
    """Model prefixes we actually ship a catalogue for, read off the directory.

    ⚠️ Not a hardcoded list: BambuStudio decides how many files exist, and a
    re-sync can add one. Sorted so a consensus answer never depends on
    filesystem order.
    """
    directory = _DATA_DIR / lang if lang != "en" else _DATA_DIR
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.json"))


def describe(device: str, full_code: str | None, short_code: str | None, lang: str = "en") -> str | None:
    """The description for one error on one model, or ``None``.

    ``full_code`` is tried first because it is lossless; ``short_code`` is the
    fallback and must be passed WITHOUT the ``XXXX_YYYY`` separator, matching how
    ``bambu_mqtt`` already calls the actions lookup.

    Returns ``None`` rather than an empty string for an uncatalogued code, so the
    caller can tell "no description" from "described as blank" — the UI shows its
    own "unrecognised code" text for the former.
    """
    catalogue = _load(lang, device)
    for code in (full_code, short_code):
        if code and code in catalogue:
            return catalogue[code]

    # ⚠️ BambuStudio ships seven catalogues and they do NOT cover the fleet by
    # serial prefix. Ours reports 01P (P1S), 030 (A1 mini) and 20P (X2D); only
    # the last has a file, so every P1S and A1 mini error resolved to nothing
    # and the operator got a bare "12FF_0001" twice over — while the text was
    # sitting in six catalogues, identical in all of them:
    # "Filament at the spool holder has run out; please insert a new filament."
    #
    # So: when the model's own catalogue cannot answer, ask the others and
    # accept the answer ONLY if they all agree. That is not the merge this
    # subsystem forbids — the ban exists because 879 codes describe different
    # mechanisms on different machines, and unanimity is exactly the test for
    # whether this code is one of them. Where they disagree we still say
    # nothing rather than guess a model.
    consensus = _consensus(lang, full_code, short_code, exclude=device)
    if consensus is not None:
        return consensus

    if lang != "en":
        return describe(device, full_code, short_code, "en")
    return None


def _consensus(lang: str, full_code: str | None, short_code: str | None, exclude: str) -> str | None:
    """One description agreed on by every catalogue that knows the code.

    ``None`` when nobody knows it, or when two models describe it differently —
    the case the per-model split exists for.
    """
    answers: set[str] = set()
    for prefix in shipped_devices(lang):
        if prefix == exclude:
            continue
        catalogue = _load(lang, prefix)
        for code in (full_code, short_code):
            if code and code in catalogue:
                answers.add(catalogue[code])
                break
    return answers.pop() if len(answers) == 1 else None


def descriptions_for(device: str, lang: str = "en") -> dict[str, str]:
    """Every description for one model, English filled in under a translation.

    One response instead of a lookup per error: the browser holds this while the
    modal is open, and a printer can report a dozen faults at once.
    """
    english = _load("en", device)
    if lang == "en":
        return english
    return {**english, **_load(lang, device)}
