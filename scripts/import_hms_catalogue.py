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
# The seven BambuStudio actually packages under resources/hms.
PACKAGED = ["093", "094", "20P", "22E", "239", "26A", "31B"]

# ⚠️ Everything else Bambu serves, which BS fetches at runtime instead of
# shipping. `HMS.cpp` keeps the same seven in `package_dev_id_types` and, for
# any other device type, calls `query.php?lang=…&d=<type>` — so a P1S or an A1
# mini has descriptions in Studio without a file on disk.
#
# Missing that half meant half the fleet had no descriptions at all: an A1 mini
# fault reached the operator as a bare "12FF_0001".
#
# ⚠️ Read off hms_actions.json rather than listed here. That file is Bambu's own
# enumeration of device types — there is no other published list, and BS itself
# only learns of a type by being handed one — so a refresh of the action
# catalogue brings any new machine's descriptions with it instead of waiting
# for someone to notice a hardcoded list is short.
HMS_QUERY_URL = "https://e.bambulab.com/query.php"
SECTIONS = ("device_hms", "device_error")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHECKOUT = REPO_ROOT / "temp" / "references" / "BambuStudio"
OUT_DIR = REPO_ROOT / "backend" / "app" / "data" / "hms"

ACTIONS_FILE = REPO_ROOT / "backend" / "app" / "data" / "hms_actions.json"


def known_device_types() -> list[str]:
    try:
        keys = set(json.loads(ACTIONS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        keys = set()
    keys.discard("default")  # the model-independent bucket, not a device
    return sorted(keys | set(PACKAGED))


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


def fetch_catalogue(device: str, lang: str) -> dict | None:
    """Ask Bambu for a device type BambuStudio does not package.

    Parity with `HMS.cpp`: it queries the same endpoint with the same two
    parameters whenever the type is outside `package_dev_id_types`. Returns
    None on any failure — a device we cannot describe is the state we were
    already in, and it must not fail the whole import.
    """
    import urllib.error
    import urllib.request

    url = f"{HMS_QUERY_URL}?lang={lang}&d={device}"
    # ⚠️ A User-Agent is not optional here: the endpoint answers curl and
    # returns 403 to urllib's default "Python-urllib/3.x". Same host our
    # action importer already talks to through requests, which sets one.
    request = urllib.request.Request(url, headers={"User-Agent": "BamDude-HMS-Import/1.0"})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed https host
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"  {device}: query failed ({exc})")
        return None
    if payload.get("result") != 0:
        print(f"  {device}: query returned result={payload.get('result')}")
        return None
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Import BambuStudio HMS descriptions")
    parser.add_argument("--bs-checkout", type=Path, default=DEFAULT_CHECKOUT)
    parser.add_argument("--lang", default="en")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="only use the BS checkout; do not ask Bambu for the device types it does not package",
    )
    args = parser.parse_args()

    hms_dir = args.bs_checkout / "resources" / "hms"
    if not hms_dir.is_dir():
        raise SystemExit(f"No BambuStudio HMS resources at {hms_dir}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    total = 0
    for prefix in known_device_types():
        src = hms_dir / f"hms_{args.lang}_{prefix}.json"
        if src.exists():
            source = json.loads(src.read_text(encoding="utf-8"))
        elif not args.offline:
            source = fetch_catalogue(prefix, args.lang)
            if source is None:
                print(f"  {prefix}: not packaged and the query returned nothing, skipped")
                continue
        else:
            print(f"  {prefix}: no {src.name} and --offline, skipped")
            continue
        catalogue = build_catalogue(source, args.lang)
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
