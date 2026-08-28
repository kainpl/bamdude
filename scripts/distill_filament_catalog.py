"""Distill a BambuStudio / OrcaSlicer checkout's BBL filament profiles into a
compact identity catalog for backend/app/data/filament_catalog/.

Usage:
    python scripts/distill_filament_catalog.py <checkout> [--out FILE] [--tag TAG]

Only the BBL vendor is read (identity catalog = the Bambu-printer ecosystem).
Values in BS profiles are single-element lists of strings; the inherits chain
is leaf -> "<family> @base" -> "fdm_filament_*" -> "fdm_filament_common".
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

IDENTITY_KEYS = (
    "filament_id",
    "setting_id",
    "filament_type",
    "filament_vendor",
    "filament_is_support",
    "nozzle_temperature_range_low",
    "nozzle_temperature_range_high",
    "compatible_printers",
)


def _scalar(value):
    """BS stores scalars as 1-element lists of strings."""
    if isinstance(value, list):
        value = value[0] if value else None
    return value


def _as_int(value):
    value = _scalar(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def load_profiles(profiles_root: Path) -> dict[str, dict]:
    """Load every BBL filament profile, keyed by its internal name.

    The vendor index (BBL.json -> filament_list[].sub_path) is the authority —
    Orca nests some profiles in subdirectories (filament/Polymaker/...) that a
    flat glob misses. The glob of BBL/filament/*.json runs as well, so files
    the index forgot are still swept up; the index wins on name collisions.
    """
    profiles: dict[str, dict] = {}

    def _read(path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"unreadable profile {path}: {e}", file=sys.stderr)
            return
        name = data.get("name") or path.stem
        profiles[name] = data

    for path in sorted((profiles_root / "BBL" / "filament").glob("*.json")):
        _read(path)

    index_path = profiles_root / "BBL.json"
    if index_path.is_file():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"unreadable vendor index {index_path}: {e}", file=sys.stderr)
            index = {}
        for entry in index.get("filament_list") or []:
            sub_path = entry.get("sub_path")
            if sub_path:
                path = profiles_root / "BBL" / sub_path
                if path.is_file():
                    _read(path)
                else:
                    print(f"vendor index names a missing file: {sub_path}", file=sys.stderr)
    return profiles


def merged_chain(profiles: dict[str, dict], name: str) -> dict:
    """Merge the inherits chain root-first so the leaf's values win."""
    chain: list[dict] = []
    seen: set[str] = set()
    current = profiles.get(name)
    while current is not None:
        chain.append(current)
        parent = current.get("inherits")
        if not parent or parent in seen:
            break
        seen.add(parent)
        current = profiles.get(parent)
    merged: dict = {}
    for layer in reversed(chain):
        for key in IDENTITY_KEYS:
            if key in layer:
                merged[key] = layer[key]
    return merged


def distill(checkout: Path, tag: str) -> tuple[dict, list[str]]:
    profiles_root = checkout / "resources" / "profiles"
    if not (profiles_root / "BBL" / "filament").is_dir():
        raise SystemExit(f"not a slicer checkout (missing {profiles_root / 'BBL' / 'filament'})")
    profiles = load_profiles(profiles_root)

    errors: list[str] = []
    families: dict[str, dict] = {}
    presets: list[dict] = []

    for name, data in profiles.items():
        merged = merged_chain(profiles, name)
        if name.endswith(" @base"):
            fid = _scalar(merged.get("filament_id"))
            if not fid:
                errors.append(f"@base without filament_id: {name}")
                continue
            families[fid] = {
                "filament_id": fid,
                "alias": name[: -len(" @base")],
                "vendor": _scalar(merged.get("filament_vendor")),
                "filament_type": _scalar(merged.get("filament_type")),
                "is_support": _scalar(merged.get("filament_is_support")) in ("1", 1, True),
            }
            continue
        if _scalar(data.get("instantiation")) != "true":
            continue
        fid = _scalar(merged.get("filament_id"))
        sid = _scalar(merged.get("setting_id"))
        if not fid or not sid:
            errors.append(f"unresolvable leaf (filament_id={fid!r} setting_id={sid!r}): {name}")
            continue
        compat = merged.get("compatible_printers") or []
        if not isinstance(compat, list):
            compat = [compat]
        presets.append(
            {
                "name": name,
                "setting_id": sid,
                "filament_id": fid,
                "compatible_printers": sorted(str(p) for p in compat),
                "nozzle_temp": [
                    _as_int(merged.get("nozzle_temperature_range_low")),
                    _as_int(merged.get("nozzle_temperature_range_high")),
                ],
            }
        )

    catalog = {
        "source": {"checkout": checkout.name, "tag": tag, "generated": date.today().isoformat()},
        "families": sorted(families.values(), key=lambda f: f["filament_id"]),
        "presets": sorted(presets, key=lambda p: p["name"]),
    }
    return catalog, errors


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
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--tag", default=None)
    args = parser.parse_args()

    tag = args.tag or detect_tag(args.checkout)
    catalog, errors = distill(args.checkout, tag)
    for line in errors:
        print(line, file=sys.stderr)

    out = args.out
    if out is None:
        name = "bambu.json" if "BambuStudio" in args.checkout.name else "orca.json"
        out = Path(__file__).resolve().parent.parent / "backend" / "app" / "data" / "filament_catalog" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(catalog, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{out}: {len(catalog['families'])} families, {len(catalog['presets'])} presets, {len(errors)} errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
