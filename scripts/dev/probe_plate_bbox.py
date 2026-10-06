"""Read-only: does Metadata/plate_N.json carry usable bbox_objects in our sliced 3MFs? (spec §5.3, E1)."""

import json
import re
import sys
import zipfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "temp" / "part-thumbnails-e1" / "plate-json-bbox.json"
# Optional data root: a worktree has no data/ of its own (gitignored), so point it at a checkout's.
DATA = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data"


def main() -> int:
    stats: Counter[str] = Counter()
    samples: list[dict] = []
    for folder in ("archive", "library"):
        for path in (DATA / folder).rglob("*.3mf"):
            try:
                archive = zipfile.ZipFile(path)
            except zipfile.BadZipFile:
                stats["not_zip"] += 1
                continue
            with archive:
                names = set(archive.namelist())
                for entry in sorted(n for n in names if re.fullmatch(r"Metadata/plate_\d+\.gcode", n)):
                    stats["sliced_plates"] += 1
                    plate = entry.removeprefix("Metadata/plate_").removesuffix(".gcode")
                    meta = f"Metadata/plate_{plate}.json"
                    if meta not in names:
                        stats["no_plate_json"] += 1
                        continue
                    try:
                        data = json.loads(archive.read(meta))
                    except ValueError:
                        stats["plate_json_unreadable"] += 1
                        continue
                    boxes = data.get("bbox_objects")
                    if not isinstance(boxes, list) or not boxes:
                        stats["no_bbox_objects"] += 1
                        continue
                    valid = [b for b in boxes if isinstance(b.get("bbox"), list) and len(b["bbox"]) == 4]
                    stats["with_bbox_objects"] += 1
                    stats["boxes"] += len(boxes)
                    stats["valid_boxes"] += len(valid)
                    if len(samples) < 20:
                        samples.append({"path": str(path.relative_to(DATA)), "plate": int(plate), "boxes": boxes[:4]})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"stats": stats, "samples": samples}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
