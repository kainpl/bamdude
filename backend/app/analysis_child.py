"""Long-lived, single-operation-at-a-time 3MF parser without broker credentials."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_ATTEMPT = re.compile(r"[0-9a-f]{32}\Z")


def main() -> int:
    bootstrap = json.loads(sys.stdin.buffer.readline(16385))
    if set(bootstrap) != {"staging", "archive_root"}:
        return 2
    staging = Path(bootstrap["staging"]).resolve(strict=True)
    archive_root = Path(bootstrap["archive_root"]).resolve(strict=True)
    from backend.app.services.analysis_codec import encode
    from backend.app.services.analysis_source import AnalysisSource, AnalysisSourceError
    from backend.app.services.print_file_analysis import _parse_3mf

    (staging / "child.ready").write_text(str(os.getpid()), encoding="ascii")

    for line in sys.stdin.buffer:
        root = None
        try:
            command = json.loads(line)
            if set(command) != {"attempt_id", "source"} or not _ATTEMPT.fullmatch(command["attempt_id"]):
                return 2
            attempt = command["attempt_id"]
            root = staging / attempt
            if not root.is_dir() or root.is_symlink():
                return 2
            source = AnalysisSource.from_payload(command["source"])
            source_path = Path(source.path).resolve(strict=True)
            source_path.relative_to(archive_root)
            if source_path != Path(source.path):
                raise AnalysisSourceError("archive source is not canonical")
            analysis = _parse_3mf(source.path, source.plate_id, source.to_payload())
            artifact = encode(analysis, identity=attempt)
            if len(artifact) > 32 * 1024 * 1024:
                raise ValueError("analysis artifact exceeds transport budget")
            with (root / "output.part").open("xb") as output:
                output.write(artifact)
                output.flush()
                os.fsync(output.fileno())
            os.replace(root / "output.part", root / "output.bin")
            result = {"outcome": "ok"}
        except Exception as exc:
            result = {
                "outcome": "resource_limit" if isinstance(exc, (ValueError, AnalysisSourceError)) else "parse_failed"
            }
        if root is None:
            return 2
        try:
            with (root / "result.part").open("x", encoding="utf-8") as output:
                json.dump(result, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(root / "result.part", root / "result.json")
        except Exception:
            return 3
        analysis = None
        artifact = None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
