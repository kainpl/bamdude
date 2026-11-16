"""No shared PostgreSQL/service: the wheel starts only under this fresh test root."""

import json
import os
import subprocess
import sys

import pytest

from backend.tests.integration.test_postgres_scenarios import REPO_ROOT


@pytest.mark.skipif(sys.platform != "win32", reason="D418 native Windows PostgreSQL leg")
@pytest.mark.timeout(480)
def test_native_embedded_postgres_restore_and_restart_completes_owned_debt(tmp_path):
    pytest.importorskip("embedded_postgres")
    root = tmp_path / "native"
    assert not root.exists()
    env = {
        **os.environ,
        "BAMDUDE_IGNORE_DOTENV": "1",
        "DATA_DIR": str(root),
        "LOG_DIR": str(root / "logs"),
        "DATABASE_URL": "embedded",
        "EMBEDDED_PG_EXTERNAL_SERVICE": "false",
        "EMBEDDED_PG_PORT": "",
        "D4_NATIVE_ROOT": str(root),
        "DEBUG": "false",
    }
    env.pop("EMBEDDED_PG_PORT", None)  # Unset chooses a free port; empty is not an integer.
    command = [sys.executable, "-m", "backend.tests.integration.native_schema_repair_runner"]
    succeeded = False
    try:
        result = subprocess.run(
            command, cwd=REPO_ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=420
        )
        assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-7000:]
        assert json.loads(result.stdout.strip().splitlines()[-1]) == {
            "windows_native": True,
            "actual_http_restore": True,
            "cold_pg_restart": True,
            "debt": None,
        }
        succeeded = True
    finally:
        if not succeeded and (root / "postgres").exists():
            # The same verified DATA_DIR is the only allowed cleanup target.
            # Never enumerate/kill arbitrary postgres processes on the machine.
            cleanup = subprocess.run(
                [*command, "stop"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=120,
            )
            assert cleanup.returncode == 0, cleanup.stderr[-3000:]
