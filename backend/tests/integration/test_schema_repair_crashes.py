"""Kill only Popen children owned by this test, on the declared scratch target."""

import json
import os
import subprocess
import sys
import time

import pytest

from backend.tests.integration.test_postgres_scenarios import (
    REPO_ROOT,
    _async_url,
    _pg_url,
    _serialize_shared_postgres_target,  # noqa: F401
    _wipe,
)

RUNNER = "backend.tests.integration.schema_repair_crash_runner"


def environment(root, url=None):
    return {
        **os.environ,
        "BAMDUDE_IGNORE_DOTENV": "1",
        "DATA_DIR": str(root),
        "LOG_DIR": str(root / "logs"),
        "DATABASE_URL": _async_url(url) if url else "",
        "DEBUG": "false",
        "D4_SIGNAL": str(root / "signal"),
    }


def run(env, mode):
    result = subprocess.run(
        [sys.executable, "-m", RUNNER, mode],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-7000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def pg_target():
    url = _pg_url()
    _wipe(url, all_objects=True)
    return url


def kill_at(root, env, phase):
    signal = root / "signal"
    signal.unlink(missing_ok=True)
    # Files own child diagnostics as well; pipe capacity cannot suspend it
    # before it reaches our intended window. No secret DATA_DIR is exported.
    with (root / "kill-child.stdout").open("wb") as stdout, (root / "kill-child.stderr").open("wb") as stderr:
        child = subprocess.Popen(
            [sys.executable, "-m", RUNNER, "kill", phase], cwd=REPO_ROOT, env=env, stdout=stdout, stderr=stderr
        )
        try:
            deadline = time.monotonic() + 150
            while not signal.exists():
                assert child.poll() is None, (root / "kill-child.stderr").read_text(encoding="utf-8")[-7000:]
                assert time.monotonic() < deadline, "owned child did not reach the named phase"
                time.sleep(0.05)
            assert signal.read_text().strip() == phase
            child.kill()
            child.wait(timeout=10)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)


@pytest.mark.postgres
@pytest.mark.timeout(330)
@pytest.mark.parametrize(
    "phase", ["replacement", "B", "D4-indexes", "columns", "UNIQUE", "FK", "model-indexes", "CHECK", "verified"]
)
def test_pg_kill_between_each_conform_phase_retries_the_whole_owned_plan(tmp_path, phase, pg_target):
    _wipe(pg_target, all_objects=True)
    env = environment(tmp_path, pg_target)
    run(env, "prepare")
    kill_at(tmp_path, env, phase)
    assert run(env, "budget")["budget"] == (2 if phase == "replacement" else 1)
    result = run(env, "resume")
    assert result == {"debt": None, "indexes": 11, "UNIQUE": True, "FK": True, "CHECK": True}


@pytest.mark.timeout(120)
@pytest.mark.parametrize("phase", ["D4-indexes", "verified"])
def test_sqlite_kill_before_debt_clear_retains_owned_repair(tmp_path, phase):
    env = environment(tmp_path)
    run(env, "prepare")
    kill_at(tmp_path, env, phase)
    assert run(env, "budget") == {"budget": 1}
    assert run(env, "resume")["debt"] is None


@pytest.mark.timeout(180)
def test_two_real_kills_force_closed_maintenance_until_one_explicit_offline_retry(tmp_path):
    env = environment(tmp_path)
    run(env, "prepare")
    for remaining in (1, 0):
        kill_at(tmp_path, env, "D4-indexes")
        assert run(env, "budget") == {"budget": remaining}
    assert run(env, "maintenance") == {"maintenance": True, "automatic_ddl": False, "farm_started": False}
    result = subprocess.run(
        [sys.executable, "-m", "backend.app.cli", "schema_repair", "status"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["automatic_attempts_remaining"] == 0
    result = subprocess.run(
        [sys.executable, "-m", "backend.app.cli", "schema_repair", "retry"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    assert run(env, "resume")["debt"] is None


@pytest.mark.postgres
@pytest.mark.timeout(330)
def test_legacy_as_is_replacement_resumes_converters_without_create_all_or_reimport(tmp_path, pg_target):
    _wipe(pg_target, all_objects=True)
    env = environment(tmp_path, pg_target)
    assert run(env, "prepare-legacy") == {"budget": 2}
    kill_at(tmp_path, env, "claimed")
    assert run(env, "budget") == {"budget": 1}
    assert run(env, "resume-legacy") == {
        "legacy_resumed": True,
        "source_not_consumed": True,
        "target_setting_retained": True,
        "debt": None,
    }
