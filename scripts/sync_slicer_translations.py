"""Extract the panel's Ukrainian strings from OrcaSlicer's pinned gettext catalog.

Development only: pip install polib==1.2.0
Run from any directory; --catalog allows offline regeneration, --check verifies it.
"""

import argparse
import hashlib
import json
import re
import urllib.request
from collections import defaultdict
from pathlib import Path

import polib

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "frontend/src/data/slicer"
COMMIT = "e2c2641acf18a1e466a833dba3c890496e24afd2"
CATALOG_SHA256 = "b118af1b5d053d9deb4355bd5af7d65343785f0ff68ab0ab0ecc75a633b579a4"
URL = f"https://raw.githubusercontent.com/OrcaSlicer/OrcaSlicer/{COMMIT}/localization/i18n/uk/OrcaSlicer_uk.po"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.catalog:
        raw = args.catalog.read_bytes()
    else:
        with urllib.request.urlopen(URL, timeout=60) as response:
            raw = response.read()
    if hashlib.sha256(raw).hexdigest() != CATALOG_SHA256:
        raise SystemExit("OrcaSlicer catalog checksum differs; review the source pin first")
    # Use gettext's established parser. Fuzzy, obsolete, plural and contextual
    # entries are not authority for the context-free labels in our schema.
    catalog = {
        entry.msgid: entry.msgstr
        for entry in polib.pofile(raw.decode("utf-8")).translated_entries()
        if not entry.msgctxt and not entry.msgid_plural
    }
    schema = json.loads((DATA / "process-schema.json").read_text(encoding="utf-8"))
    tree = json.loads((DATA / "process-ui-tree.json").read_text(encoding="utf-8"))
    strings = set()
    for option in schema.values():
        for field in ("label", "tooltip", "sidetext"):
            text = option.get(field)
            if text and "->" not in text and "::" not in text:
                strings.add(text)
        strings.update(option.get("enum_labels", []))
    for page in tree:
        strings.add(page["page"])
        strings.update(group["group"] for group in page["groups"])

    # The C++ metadata extractor retains escaped apostrophes/unicode, printf
    # percent escapes and different line wrapping. Match only the same words,
    # never a similar tooltip whose numerical advice may have changed.
    def source_words(text):
        text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), text)
        return " ".join(text.replace("\\'", "'").replace("%%", "%").split())

    by_words = defaultdict(set)
    for source, target in catalog.items():
        by_words[source_words(source)].add(target)
    supplements = json.loads((DATA / "process-uk-overrides.json").read_text(encoding="utf-8"))
    unused = set(supplements) - strings
    if unused:
        raise SystemExit(f"Remove or review {len(unused)} obsolete supplemental translations")
    result, missing = {}, []
    for source in sorted(strings):
        target = supplements.get(source) or catalog.get(source)
        if not target:
            candidates = by_words.get(source_words(source), set())
            if len(candidates) == 1:
                target = next(iter(candidates))
        if not target and source.isdecimal():
            target = source
        if not target and re.fullmatch(r"\d+ mm", source):
            target = source.removesuffix("mm") + catalog["mm"]
        if not target:
            missing.append(source)
        else:
            result[source] = target
    if missing:
        raise SystemExit("Missing translations:\n" + "\n".join(missing))
    text = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    output = DATA / "process-uk.json"
    if args.check:
        if not output.exists() or output.read_text(encoding="utf-8") != text:
            raise SystemExit("process-uk.json needs regeneration")
    else:
        output.write_text(text, encoding="utf-8", newline="\n")
    print(f"{len(result)} Ukrainian strings; {len(supplements)} reviewed supplements; catalog {COMMIT}")


if __name__ == "__main__":
    main()
