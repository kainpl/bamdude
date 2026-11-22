"""Restore a manifest-backed backup containing Windows DB paths on POSIX."""

import os

import pytest

from backend.tests.integration.test_portable_backup_postgres import run
from backend.tests.integration.test_postgres_scenarios import _pg_url, _wipe


@pytest.mark.skipif(os.name == "nt", reason="Windows natively resolves backslashes")
@pytest.mark.parametrize("target", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)])
def test_windows_backup_restores_managed_paths(tmp_path, target):
    url = _pg_url() if target == "postgresql" else None
    if url:
        _wipe(url)
    source = run("export-windows-paths", tmp_path / "source", None, tmp_path)["zip"]
    result = run("restore-windows-paths", tmp_path / target, url, source)
    assert result == {"paths": "verified", "dialect": target}
