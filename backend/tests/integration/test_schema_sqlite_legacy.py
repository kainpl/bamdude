"""D4 P1: SQLite keeps its create_all-dependent legacy chain, unlike PG AS-IS."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing

import pytest

from backend.app.migrations import _discover_migrations
from backend.tests.integration.test_postgres_scenarios import REPO_ROOT


def environment(root):
    return {
        **os.environ,
        "BAMDUDE_IGNORE_DOTENV": "1",
        "DATA_DIR": str(root),
        "LOG_DIR": str(root / "logs"),
        "DATABASE_URL": "",
        "DEBUG": "false",
    }


def run(root, mode, fixture="v040"):
    result = subprocess.run(
        [sys.executable, "-m", "backend.tests.integration.schema_sqlite_legacy_runner", mode, fixture],
        cwd=REPO_ROOT,
        env=environment(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert result.returncode == 0, result.stdout[-1000:] + result.stderr[-6000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.timeout(480)
@pytest.mark.parametrize("fixture", ["v040", "v042", "v051", "v061", "pre075", "pre144"])
def test_real_legacy_sqlite_debt_boot_matches_native_order(tmp_path, fixture):
    control = run(tmp_path / "control", "control", fixture)
    restored = run(tmp_path / "restored", "debt", fixture)
    assert restored == control
    assert restored["level"] == max(m["version"] for m in _discover_migrations())


@pytest.mark.timeout(240)
def test_oldest_sqlite_signed_admin_http_restore_and_restart(tmp_path):
    result = run(tmp_path / "http", "http")
    assert result["debt"] is None
    assert result["level"] == max(m["version"] for m in _discover_migrations())


@pytest.mark.timeout(240)
@pytest.mark.parametrize("preparation", ["prepare-cli", "prepare-partial-cli"])
def test_offline_cli_can_repair_oldest_sqlite_with_exhausted_budget(tmp_path, preparation):
    root = tmp_path / "cli"
    assert run(root, preparation) == {"budget": 0}
    if preparation == "prepare-partial-cli":
        assert run(root, "assert-exhausted") == {"automatic_start_refused": True, "ddl": 0}
    result = subprocess.run(
        [sys.executable, "-m", "backend.app.cli", "schema_repair", "retry"],
        cwd=REPO_ROOT,
        env=environment(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    assert run(root, "resume")["debt"] is None


@pytest.mark.timeout(240)
def test_residual_retry_failure_does_not_fail_native_startup(tmp_path):
    assert run(tmp_path / "residual", "residual-failure") == {"startup_completed": True, "new_hard_debt": False}


@pytest.mark.timeout(240)
def test_actual_cli_exit_is_nonzero_when_optional_retry_is_incomplete(tmp_path):
    root = tmp_path / "optional-cli"
    record = run(root, "prepare-optional-cli")
    result = subprocess.run(
        [sys.executable, "-m", "backend.tests.integration.schema_sqlite_legacy_runner", "cli-residual-failure"],
        cwd=REPO_ROOT,
        env=environment(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "Residual guard retry failed; records retained" in result.stderr
    assert "DO NOT PRINT DRIVER VALUES" not in result.stderr
    assert "Schema repair verified" not in result.stdout
    with closing(sqlite3.connect(root / "bamdude.db")) as db:
        assert (
            json.loads(
                db.execute("SELECT value FROM settings WHERE key='_bamdude.schema_repair_residuals'").fetchone()[0]
            )
            == record
        )
        assert db.execute("SELECT count(*) FROM settings WHERE key='_bamdude.schema_repair_debt'").fetchone()[0] == 0
