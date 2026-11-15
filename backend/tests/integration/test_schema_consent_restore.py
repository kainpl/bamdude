"""Real HTTP/DB/file swap, synthetic source/actor and isolated DATA_DIR only."""

import json
import os
import subprocess
import sys

import pytest

from backend.tests.integration.test_postgres_scenarios import (
    REPO_ROOT,
    _async_url,
    _pg_url,
    _serialize_shared_postgres_target,  # noqa: F401
    _wipe,
)


@pytest.mark.timeout(300)
@pytest.mark.parametrize("dialect", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)])
def test_signed_admin_confirms_exact_calibration_residual_and_restores_same_rows(tmp_path, dialect):
    url = _pg_url() if dialect == "postgresql" else None
    if url:
        _wipe(url, all_objects=True)
    env = {
        **os.environ,
        "BAMDUDE_IGNORE_DOTENV": "1",
        "DATA_DIR": str(tmp_path),
        "LOG_DIR": str(tmp_path / "logs"),
        "DATABASE_URL": _async_url(url) if url else "",
        "DEBUG": "false",
    }
    result = subprocess.run(
        [sys.executable, "-m", "backend.tests.integration.schema_consent_runner"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=270,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-7000:]
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "refusal_before_drain": True,
        "confirmed_http_restore": True,
        "same_rows_and_flags": True,
        "durable_audit": True,
        "targeted_repair": True,
        "dialect": dialect,
    }
