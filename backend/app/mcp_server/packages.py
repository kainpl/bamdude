"""Deterministic portable skill asset, built from the canonical source."""

import hashlib
import io
import json
import zipfile
from pathlib import Path

from backend.app.core.config import APP_VERSION

SKILL_ROOT = Path(__file__).resolve().parents[3] / "integrations" / "agent-skills" / "bamdude-diagnostics"
PACKAGE_ROOT = Path(__file__).resolve().parents[3] / "integrations" / "ai-plugins" / "dist"
PACKAGE_FILES = {
    "claude-desktop": "bamdude-claude-desktop.mcpb",
    "claude-code": "bamdude-claude-code.zip",
    "portable-plugin": "bamdude-portable-plugin.zip",
}
MAX_PACKAGE = 16 * 1024 * 1024


def _bounded_file(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError("package_too_large")
    return content


def client_package(package_id: str, root: Path = PACKAGE_ROOT) -> tuple[bytes, str, str]:
    """Only known build products, matching this server and canonical guidance."""
    if package_id not in PACKAGE_FILES:
        raise KeyError(package_id)
    manifest = json.loads(_bounded_file(root / "packages.json", 65536))
    guide_hash = hashlib.sha256((SKILL_ROOT / "references/diagnostics.md").read_bytes()).hexdigest()
    skill_hash = hashlib.sha256((SKILL_ROOT / "SKILL.md").read_bytes()).hexdigest()
    if (
        manifest["package_version"] != APP_VERSION
        or manifest["diagnostic_contract"] != 1
        or manifest["skill_contract"] != 1
        or manifest["guide_sha256"] != guide_hash
        or manifest["skill_sha256"] != skill_hash
    ):
        raise ValueError("package_contract_mismatch")
    filename = PACKAGE_FILES[package_id]
    content = _bounded_file(root / filename, MAX_PACKAGE)
    digest = hashlib.sha256(content).hexdigest()
    if manifest["sha256"][filename] != digest:
        raise ValueError("package_hash_mismatch")
    return content, digest, filename


def package_catalog():
    try:
        skill_archive()
        skill_available = True
    except (OSError, ValueError):
        skill_available = False
    result = [
        {
            "id": "skill",
            "format": "agent-skill-zip",
            "available": skill_available,
            "reason": None if skill_available else "package_unavailable_or_incompatible",
        }
    ]
    for package_id, filename in PACKAGE_FILES.items():
        try:
            client_package(package_id)
            available, reason = True, None
        except (OSError, ValueError, KeyError, TypeError):
            available, reason = False, "package_unavailable_or_incompatible"
        result.append(
            {
                "id": package_id,
                "format": "mcpb" if filename.endswith(".mcpb") else "plugin-zip",
                "available": available,
                "reason": reason,
            }
        )
    return {"package_version": APP_VERSION, "packages": result}


def skill_archive(root: Path = SKILL_ROOT) -> tuple[bytes, str]:
    contents = {name: (root / name).read_bytes() for name in ("SKILL.md", "references/diagnostics.md")}
    if any(len(content) > 65536 for content in contents.values()):
        raise ValueError("skill_asset_too_large")
    manifest = {
        "package_version": APP_VERSION,
        "skill_contract": 1,
        "diagnostic_contract": 1,
        "supported_modules": ["base", "diagnostics"],
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
