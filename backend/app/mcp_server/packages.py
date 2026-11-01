"""Deterministic portable skill asset, built from the canonical source."""

import hashlib
import io
import json
import zipfile
from pathlib import Path

from backend.app.core.config import APP_VERSION

SKILL_ROOT = Path(__file__).resolve().parents[3] / "integrations" / "agent-skills" / "bamdude-diagnostics"


def skill_archive(root: Path = SKILL_ROOT) -> tuple[bytes, str]:
    contents = {name: (root / name).read_bytes() for name in ("SKILL.md", "references/diagnostics.md")}
    if any(len(content) > 65536 for content in contents.values()):
        raise ValueError("skill_asset_too_large")
    manifest = {
        "package_version": APP_VERSION,
        "skill_contract": 1,
        "diagnostic_contract": 1,
        "supported_modules": ["base"],
        "sha256": {name: hashlib.sha256(content).hexdigest() for name, content in contents.items()},
    }
    contents["manifest.json"] = json.dumps(manifest, sort_keys=True).encode()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(contents.items()):
            info = zipfile.ZipInfo("bamdude-diagnostics/" + name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)
    body = stream.getvalue()
    return body, hashlib.sha256(body).hexdigest()
