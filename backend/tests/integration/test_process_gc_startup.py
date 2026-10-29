"""The start-up boundary runs first, once, in a process of its own (spec process-gc-latency r2).

The suite runs with the policy off (conftest) and must not freeze a pytest worker, so every case
here spawns ``process_gc_child`` in a fresh interpreter: the opt-out is REMOVED from its
environment, ``conftest`` is never imported, and it enters the real ``main.lifespan`` over a
temporary DATA_DIR whose database ``init_db`` really opens. A green case must show the policy
actually froze (``enabled``, a non-zero frozen count) — a policy that silently stayed off proves
nothing here.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[3]


def _child(tmp_path, scenario, policy=None):
    env = {key: value for key, value in os.environ.items() if key != "BAMDUDE_GC_STARTUP_FREEZE"}
    if policy is not None:
        env["BAMDUDE_GC_STARTUP_FREEZE"] = policy
    env.update(
        BAMDUDE_IGNORE_DOTENV="1",
        DATABASE_URL="",
        DATA_DIR=str(tmp_path),
        LOG_DIR=str(tmp_path),
        LOG_TO_FILE="false",
        DEBUG="false",
        TELEMETRY_DISABLED="1",
        PYTHONIOENCODING="utf-8",
    )
    done = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "backend.tests.integration.process_gc_child", scenario],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=240,
    )
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    line = next(x for x in done.stdout.splitlines() if x.startswith("RESULT="))
    return json.loads(line[len("RESULT=") :])


def _import_changes_nothing(out):
    """CPython 3.12 starts with a frozen set of its own; importing ``main`` adds nothing to it."""
    assert out["conftest_imported"] is False
    assert out["after_import"] == out["before_import"]


def _enabled(out):
    outcome = out["outcome"]
    assert outcome["state"] == "enabled", out
    assert outcome["reason"] is None
    assert outcome["frozen"] > 100_000  # the imported application, not a handful of objects
    assert out["freeze_calls"] == 1
    # Reference counting still frees an acyclic frozen object, so the count may only fall.
    assert out["after_lifespan"]["frozen"] <= outcome["frozen"]
    assert out["after_lifespan"]["enabled"] is True
    assert out["after_lifespan"]["threshold"] == out["before_import"]["threshold"]
    assert [level for level, _ in out["records"]] == ["INFO"]


def test_the_first_start_freezes_before_the_guard_the_database_and_the_farm(tmp_path):
    """G-A01 / G-A02: importing ``main`` changes nothing; the first lifespan freezes once, and the
    farm guard, the first database connection and the farm itself all come after it."""
    out = _child(tmp_path, "first")
    _import_changes_nothing(out)
    _enabled(out)
    at_import = out["after_import"]["frozen"]
    kinds = [kind for kind, _ in out["marks"]]
    assert kinds[:3] == ["guard_start", "farm_start", "db_connect"]
    # Each came after the freeze: the application's heap was already in the permanent generation.
    assert all(count > at_import + 100_000 for _, count in out["marks"]), out["marks"]
    assert out["sentinels_alive"] == [False]


def test_a_second_lifespan_never_freezes_again(tmp_path):
    """G-A03: the state is the process's; the second start finds it decided."""
    out = _child(tmp_path, "twice")
    _enabled(out)
    assert [kind for kind, _ in out["marks"]].count("guard_start") == 2
    assert out["sentinels_alive"] == [False, False]


def test_a_failed_start_never_lets_a_later_one_freeze_its_leftovers(tmp_path):
    """G-A03: the start that failed after the boundary was the process's attempt; the next start
    freezes nothing, and the failed start's objects stay collectable."""
    out = _child(tmp_path, "failed_then_again")
    assert out["first_start_failed"] == "start-up failed after the boundary"
    _enabled(out)
    assert out["sentinels_alive"] == [False, False]


def test_a_restore_rebuilds_the_engine_without_freezing_anything(tmp_path):
    """G-A03: a restore swaps the engine and the session factory; the boundary is not asked again
    and what the restored database's session creates is collected as usual."""
    out = _child(tmp_path, "restore")
    _enabled(out)
    assert [kind for kind, _ in out["marks"]].count("db_connect") >= 2
    assert out["sentinels_alive"] == [False, False]


@pytest.mark.parametrize(
    ("scenario", "policy", "reason", "level"),
    [
        ("first", "off", "disabled_by_env", "INFO"),
        ("host_disabled", None, "gc_disabled", "WARNING"),
        ("host_frozen", None, "already_frozen", "WARNING"),
        ("late", None, "late_start", "WARNING"),
    ],
)
def test_a_skip_leaves_the_collector_as_the_host_has_it(tmp_path, scenario, policy, reason, level):
    """G-A04: off, a host's own collector state and a process already working: nothing changes,
    one record says why, and nothing claims success."""
    out = _child(tmp_path, scenario, policy)
    assert out["outcome"]["state"] == "skipped"
    assert out["outcome"]["reason"] == reason
    assert out["freeze_calls"] == 0
    before, after = out["after_import"], out["after_lifespan"]
    assert (after["enabled"], after["threshold"]) == (before["enabled"], before["threshold"])
    assert after["frozen"] <= before["frozen"]
    assert [lvl for lvl, _ in out["records"]] == [level]
    assert reason in out["records"][0][1]


def test_an_unknown_setting_refuses_the_start_before_the_farm_guard(tmp_path):
    """G-R6: a typo is a configuration error before the farm starts, never a silent default."""
    out = _child(tmp_path, "first", "maybe")
    assert "BAMDUDE_GC_STARTUP_FREEZE" in out["config_error"]
    assert out["marks"] == []
    assert out["freeze_calls"] == 0
    assert out["outcome"] is None
    assert out["after_lifespan"]["frozen"] <= out["after_import"]["frozen"]
