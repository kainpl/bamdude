"""Re-copy backend/app/data/printers/*.json byte-for-byte from a BambuStudio
checkout (resources/printers/). Mirrors only the files that already exist in
the destination — adding a new model stays a deliberate manual act. Updates
the "Source tag:" line in the folder README.

Usage: python scripts/sync_printer_configs.py <bs_checkout> [--dest DIR] [--tag TAG]
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path


def detect_tag(checkout: Path) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(checkout), "describe", "--tags", "--always"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument(
        "--dest",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "backend" / "app" / "data" / "printers",
    )
    parser.add_argument("--tag", default=None)
    args = parser.parse_args()

    src_dir = args.checkout / "resources" / "printers"
    if not src_dir.is_dir():
        raise SystemExit(f"not a BambuStudio checkout (missing {src_dir})")
    tag = args.tag or detect_tag(args.checkout)

    changed, missing = [], []
    for dst in sorted(args.dest.glob("*.json")):
        src = src_dir / dst.name
        if not src.is_file():
            missing.append(dst.name)
            continue
        # "Byte-for-byte" modulo the trailing newline: BS ships its JSONs
        # without one and our pre-commit end-of-file fixer adds it back, so a
        # newline-only difference is the mirror's steady state, not a change.
        if src.read_bytes().rstrip(b"\r\n") != dst.read_bytes().rstrip(b"\r\n"):
            shutil.copyfile(src, dst)
            changed.append(dst.name)

    readme = args.dest / "README.md"
    if readme.is_file():
        text = readme.read_text(encoding="utf-8")
        # Two tag formats: the simple "Source tag: vX" and the repo README's
        # "- **Source:** BambuStudio `resources/printers/` @ tag **`vX`**".
        new_text = re.sub(r"Source tag: \S+", f"Source tag: {tag}", text)
        new_text = re.sub(r"(@ tag \*\*`)[^`]+(`\*\*)", rf"\g<1>{tag}\g<2>", new_text)
        if new_text != text:
            readme.write_text(new_text, encoding="utf-8")

    for name in changed:
        print(f"updated: {name}")
    for name in missing:
        print(f"MISSING upstream (kept ours): {name}", file=sys.stderr)
    print(f"{len(changed)} updated, {len(missing)} missing, tag {tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
