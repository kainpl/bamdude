"""Import BambuStudio's HMS error descriptions into BamDude's data files.

BambuStudio ships ``resources/hms/hms_{lang}_{prefix}.json`` per model, where
``prefix`` is the first three characters of a printer's serial number. Each file
holds two sections, and they are keyed DIFFERENTLY:

* ``data.device_hms.{lang}[]``   — ``ecode`` is a **16-character** full code
* ``data.device_error.{lang}[]`` — ``ecode`` is an **8-character** short code

⚠️ **Both halves are needed.** BamDude's previous descriptions came from the
short-code half alone, which is why they intersected ``device_error`` in 692
places and ``device_hms`` in **zero** — while ``device_hms`` is the half that
printers actually report. A print that could not record its timelapse said so
with `0500010000030004`, and BamDude had no text for it, anywhere.

⚠️ **Per model, and deliberately not deduplicated.** 879 codes carry different
text on different machines — `0C00020000010001` is a horizontal laser on one and
a height-measuring laser on another — so a merged catalogue would confidently
describe the wrong mechanism. The redundancy is the price of being right, and it
is only ~4 MB.

⚠️ Deduplication belongs to the TRANSLATION pipeline, not to storage: the same
sentence repeats across up to seven models, so translating the distinct set is a
fifth of the work for an identical result. See ``translate_hms_catalogue.py``.

Usage:
    python scripts/import_hms_catalogue.py [--bs-checkout PATH] [--lang en]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# The models BambuStudio ships a catalogue for, by serial-number prefix.
PREFIXES = ["093", "094", "20P", "22E", "239", "26A", "31B"]
SECTIONS = ("device_hms", "device_error")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHECKOUT = REPO_ROOT / "temp" / "references" / "BambuStudio"
OUT_DIR = REPO_ROOT / "backend" / "app" / "data" / "hms"


def build_catalogue(source: dict, lang: str) -> dict[str, str]:
    """Flatten one BambuStudio file into ``{ecode: description}``.

    Entries with no text are dropped: an empty description renders as a blank
    line where the "unrecognised code" fallback would otherwise have told the
    operator something useful.

    Tolerates a missing section, a missing language and a missing ``data`` key —
    not every prefix ships both halves, and asking for a language BS does not
    carry (``uk``, for one) must be empty rather than a crash.
    """
    data = source.get("data") or {}
    out: dict[str, str] = {}
    for section in SECTIONS:
        for entry in (data.get(section) or {}).get(lang) or []:
            ecode = (entry.get("ecode") or "").strip()
            intro = (entry.get("intro") or "").strip()
            if ecode and intro:
                out[ecode] = intro
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Import BambuStudio HMS descriptions")
    parser.add_argument("--bs-checkout", type=Path, default=DEFAULT_CHECKOUT)
    parser.add_argument("--lang", default="en")
    args = parser.parse_args()

    hms_dir = args.bs_checkout / "resources" / "hms"
    if not hms_dir.is_dir():
        raise SystemExit(f"No BambuStudio HMS resources at {hms_dir}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    total = 0
    for prefix in PREFIXES:
        src = hms_dir / f"hms_{args.lang}_{prefix}.json"
        if not src.exists():
            print(f"  {prefix}: no {src.name}, skipped")
            continue
        catalogue = build_catalogue(json.loads(src.read_text(encoding="utf-8")), args.lang)
        dest = OUT_DIR / f"{prefix}.json"
        dest.write_text(
            json.dumps(catalogue, ensure_ascii=False, indent=0, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        total += len(catalogue)
        print(f"  {prefix}: {len(catalogue):>5} entries -> {dest.relative_to(REPO_ROOT)}")
    print(f"  total: {total} entries")


if __name__ == "__main__":
    main()
