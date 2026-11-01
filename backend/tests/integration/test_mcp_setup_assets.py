"""Operator setup uses explicit addresses and authenticated, matching build assets."""

import hashlib
import json

import pytest

from backend.app.mcp_server import packages


async def test_canonical_endpoint_is_saved_not_derived_from_request(async_client):
    saved = await async_client.patch("/api/v1/settings/", json={"mcp_endpoint_url": "https://Farm.Example:8443/mcp"})
    assert saved.status_code == 200, saved.text
    assert saved.json()["mcp_endpoint_url"] == "https://farm.example:8443/mcp"
    read = await async_client.get("/api/v1/settings/", headers={"Host": "different.example"})
    assert read.json()["mcp_endpoint_url"] == "https://farm.example:8443/mcp"


@pytest.mark.parametrize(
    "address",
    [
        "http://farm.local/mcp",
        "https://a:b@farm.local/mcp",
        "https://farm.local/api",
        "https://farm.local/mcp?key=x",
        "https://farm.local/mcp#secret",
        None,
    ],
)
async def test_invalid_canonical_endpoint_is_rejected(async_client, address):
    response = await async_client.patch("/api/v1/settings/", json={"mcp_endpoint_url": address})
    assert response.status_code == 422, response.text


def matching_manifest(tmp_path):
    filename = packages.PACKAGE_FILES["claude-desktop"]
    body = b"synthetic versioned build artifact"
    digest = hashlib.sha256(body).hexdigest()
    (tmp_path / filename).write_bytes(body)
    manifest = {
        "package_version": packages.APP_VERSION,
        "diagnostic_contract": 1,
        "skill_contract": 1,
        "guide_sha256": hashlib.sha256((packages.SKILL_ROOT / "references/diagnostics.md").read_bytes()).hexdigest(),
        "skill_sha256": hashlib.sha256((packages.SKILL_ROOT / "SKILL.md").read_bytes()).hexdigest(),
        "sha256": {filename: digest},
    }
    (tmp_path / "packages.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest, body, digest


async def test_catalog_and_download_use_only_matching_allowlisted_packages(async_client, tmp_path, monkeypatch):
    manifest, body, digest = matching_manifest(tmp_path)
    original = packages.client_package
    monkeypatch.setattr(packages, "client_package", lambda name: original(name, tmp_path))
    catalog = await async_client.get("/api/v1/integrations/mcp/packages")
    assert catalog.status_code == 200
    by_id = {item["id"]: item for item in catalog.json()["packages"]}
    assert by_id["skill"]["available"]
    assert by_id["claude-desktop"]["available"]
    assert not by_id["claude-code"]["available"]
    downloaded = await async_client.get("/api/v1/integrations/mcp/packages/claude-desktop")
    assert downloaded.status_code == 200 and downloaded.content == body
    assert downloaded.headers["x-content-sha256"] == digest
    assert downloaded.headers["cache-control"] == "no-store"
    assert (await async_client.get("/api/v1/integrations/mcp/packages/not-an-asset")).status_code == 404
    manifest["sha256"] = {}
    (tmp_path / "packages.json").write_text(json.dumps(manifest))
    assert (await async_client.get("/api/v1/integrations/mcp/packages/claude-desktop")).status_code == 503


@pytest.mark.parametrize(
    "field", ["package_version", "diagnostic_contract", "skill_contract", "guide_sha256", "skill_sha256"]
)
def test_version_contract_and_canonical_source_mismatch_refuse_package(tmp_path, field):
    manifest, _, _ = matching_manifest(tmp_path)
    manifest[field] = "mismatch"
    (tmp_path / "packages.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="package_contract_mismatch"):
        packages.client_package("claude-desktop", tmp_path)


def test_corrupt_package_and_oversized_file_are_refused(tmp_path):
    _, _, _ = matching_manifest(tmp_path)
    path = tmp_path / packages.PACKAGE_FILES["claude-desktop"]
    path.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="package_hash_mismatch"):
        packages.client_package("claude-desktop", tmp_path)
    with path.open("wb") as file:
        file.truncate(packages.MAX_PACKAGE + 1)
    with pytest.raises(ValueError, match="package_too_large"):
        packages.client_package("claude-desktop", tmp_path)


async def test_setup_and_download_require_the_existing_read_scope(async_client):
    from backend.tests.integration.test_mcp_base_transport import key

    created = await key(async_client)
    changed = await async_client.patch(f"/api/v1/api-keys/{created['id']}", json={"can_read_status": False})
    assert changed.status_code == 200
    for path in ("/status", "/packages", "/packages/claude-desktop", "/skill.zip"):
        response = await async_client.get(
            "/api/v1/integrations/mcp" + path, headers={"Authorization": f"Bearer {created['key']}"}
        )
        assert response.status_code == 403, response.text
