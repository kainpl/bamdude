"""Produce the Ukrainian HMS catalogue from the English one.

BambuStudio ships sixteen languages and Ukrainian is not among them, so this one
is ours to make.

⚠️ **Deduplicate for translation, never for storage.** The stored files stay
per-model and un-deduplicated — 879 codes carry different text on different
machines (see ``backend/app/data/hms/README.md``). But the same sentence repeats
across up to seven models: 36 522 stored strings are 6 378 distinct ones, so
translating the distinct set and expanding it back costs a fifth of the work for
an identical result.

The translation itself does not happen here. ``--export`` writes every distinct
string to a work file for a translator — human or machine — and ``--import``
reads the filled-in file back and lays the results out per model.

⚠️ A string left blank in the work file is **skipped**, not written as English.
The lookup already falls back to English, and copying it in would make a
half-finished translation indistinguishable from a complete one.

Usage:
    python scripts/translate_hms_catalogue.py --export   # writes the work file
    python scripts/translate_hms_catalogue.py --import   # writes backend/app/data/hms/uk/
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EN_DIR = REPO_ROOT / "backend" / "app" / "data" / "hms"
UK_DIR = EN_DIR / "uk"
# temp/ is gitignored: the work file is scaffolding, only the result is kept.
WORK_FILE = REPO_ROOT / "temp" / "hms-translation" / "untranslated.json"


def shipped_prefixes() -> list[str]:
    """Every model we ship an English catalogue for, read off the directory.

    ⚠️ Not a list written here. It was one — the seven BambuStudio packages —
    and when the importer started fetching the other seven device types from
    Bambu (00M, 01P, 030, …) this script kept reporting "0 still to translate"
    while 18 000 new descriptions sat beside it untranslated. A hardcoded set
    does not fail, it under-reports.
    """
    return sorted(p.stem for p in EN_DIR.glob("*.json"))


def load_english() -> dict[str, dict[str, str]]:
    """Every model's English catalogue, keyed by prefix."""
    out: dict[str, dict[str, str]] = {}
    for prefix in shipped_prefixes():
        path = EN_DIR / f"{prefix}.json"
        if path.exists():
            out[prefix] = json.loads(path.read_text(encoding="utf-8"))
    return out


def distinct_strings(catalogues: dict[str, dict[str, str]]) -> set[str]:
    """Every different sentence across every model, once."""
    return {text for catalogue in catalogues.values() for text in catalogue.values()}


def expand(catalogues: dict[str, dict[str, str]], translations: dict[str, str]) -> dict[str, dict[str, str]]:
    """Lay translated sentences back out under every code that used them.

    A string with no translation — or a blank one — is omitted rather than
    copied in English: the lookup already falls back, and copying would hide
    which strings still need work.

    A model with nothing translated yields no entry at all, so ``--import``
    writes no file for it. An empty file would be loaded and cached for nothing,
    and would look like a finished translation with no matches.
    """
    out: dict[str, dict[str, str]] = {}
    for prefix, catalogue in catalogues.items():
        translated = {
            code: translations[text].strip() for code, text in catalogue.items() if translations.get(text, "").strip()
        }
        if translated:
            out[prefix] = translated
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Translate the HMS catalogue into Ukrainian")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--export", action="store_true", help="write the distinct strings for translating")
    group.add_argument("--import", dest="do_import", action="store_true", help="read them back and write uk/")
    args = parser.parse_args()

    catalogues = load_english()
    if not catalogues:
        raise SystemExit(f"No English catalogue in {EN_DIR} — run scripts/import_hms_catalogue.py first")

    if args.export:
        strings = sorted(distinct_strings(catalogues))
        existing: dict[str, str] = {}
        if WORK_FILE.exists():
            # Keep work already done: a BS refresh adds strings, it does not
            # invalidate the ones already translated.
            existing = json.loads(WORK_FILE.read_text(encoding="utf-8"))
        WORK_FILE.parent.mkdir(parents=True, exist_ok=True)
        WORK_FILE.write_text(
            json.dumps({s: existing.get(s, "") for s in strings}, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8",
        )
        todo = sum(1 for s in strings if not existing.get(s, "").strip())
        chars = sum(len(s) for s in strings if not existing.get(s, "").strip())
        stored = sum(len(c) for c in catalogues.values())
        print(f"  {len(strings)} distinct strings out of {stored} stored")
        print(f"  {todo} still to translate ({chars:,} characters) -> {WORK_FILE}")
        return

    if not WORK_FILE.exists():
        raise SystemExit(f"No work file at {WORK_FILE} — run with --export first")

    translations = json.loads(WORK_FILE.read_text(encoding="utf-8"))
    UK_DIR.mkdir(parents=True, exist_ok=True)
    result = expand(catalogues, translations)
    for prefix, catalogue in result.items():
        dest = UK_DIR / f"{prefix}.json"
        dest.write_text(json.dumps(catalogue, ensure_ascii=False, indent=0, sort_keys=True) + "\n", encoding="utf-8")
        print(f"  {prefix}: {len(catalogue):>5} translated -> {dest.relative_to(REPO_ROOT)}")
    if not result:
        print("  nothing translated yet — fill in the work file first")


if __name__ == "__main__":
    main()
