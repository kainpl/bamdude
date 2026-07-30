"""A brand-new install must boot: ``create_all()`` plus the whole migration
chain, against a blank database, in one process — exactly what a first-time
user gets.

**Why this exists.** Nothing else in the ordinary test run does it. ``init_db()``
on an empty database appeared only in ``postgres_scenario_runner``, which needs
a PostgreSQL service container and is therefore skipped on every developer
machine and in the plain Backend Tests job. So the one scenario every new user
hits was covered exclusively by a job most runs never execute.

That gap has shipped twice. ``v0.4.5b2`` was cut as "m062 fresh-install boot
fix", and 0.5.1.2 shipped a ``:latest`` that no fresh install could start: m116
returned ``require_previous_success`` to the model, which m002 had been using as
its "this is a legacy table" sentinel, so m002 fired on a fresh schema and died
copying a column m007 had dropped. Both were the same shape — a later migration
invalidating an earlier one's assumption about what the schema looks like — and
both are invisible to migration-level unit tests, which build their own little
table and never see the real ``create_all`` output.

The assertion is deliberately end-to-end rather than aimed at m002. The class of
bug is "some migration cannot run against the current model", and only booting
the real chain finds the next one.

Runs as a subprocess because ``core.database`` builds its engine at import time
from ``settings``: the target database cannot be switched inside a running
interpreter, and setting the environment first is how the application itself
starts.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = REPO_ROOT / "backend" / "app" / "migrations"

_BOOT = """
import asyncio, json

async def main():
    from backend.app.core.database import engine, init_db
    from sqlalchemy import text

    await init_db()
    async with engine.begin() as conn:
        applied = [r[0] for r in (await conn.execute(text("SELECT version FROM _migrations"))).all()]
    await engine.dispose()
    return applied

print("APPLIED:" + json.dumps(sorted(asyncio.run(main()))))
"""


def _declared_versions() -> set[int]:
    """Every migration the package ships, via the loader's own discovery.

    Deliberately the same function ``init_db`` runs (``_discover_migrations``)
    rather than a filename scan: it reads each module's real ``version``, so a
    file whose name and ``version`` disagree cannot slip through, and the test
    can never disagree with the loader about what "ships" means.
    """
    from backend.app.migrations import _discover_migrations

    versions = {m["version"] for m in _discover_migrations()}
    assert versions, "no migrations discovered - the loader found nothing to run"
    return versions


def test_fresh_sqlite_install_boots(tmp_path):
    """The whole chain applies to a blank SQLite, and every migration lands."""
    db = tmp_path / "bamdude.db"
    env = {
        **os.environ,
        "DATA_DIR": str(tmp_path),
        "DATABASE_URL": f"sqlite+aiosqlite:///{db.as_posix()}",
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONIOENCODING": "utf-8",
    }
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _BOOT],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )

    assert proc.returncode == 0, (
        f"a fresh install failed to boot — this is what a new user gets\n"
        f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
    )

    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("APPLIED:")), None)
    assert line is not None, f"boot produced no migration report\nstdout:\n{proc.stdout}"
    applied = set(json.loads(line.removeprefix("APPLIED:")))

    missing = _declared_versions() - applied
    assert not missing, f"migrations never ran on a fresh install: {sorted(missing)}"


def test_m002_legacy_sentinel_cannot_be_a_live_model_column():
    """m002 decides "this is a legacy table" from columns the model must not have.

    The guard-rail for the specific mistake above: if a future migration returns
    ``vibration_cali`` to ``PrintQueueItem`` the way m116 returned
    ``require_previous_success``, m002 starts misfiring on fresh installs again —
    and the failure is a boot crash, not a test failure, unless something checks
    here.
    """
    from backend.app.models.print_queue import PrintQueueItem

    source = (MIGRATIONS_DIR / "m002_bamdude_311.py").read_text(encoding="utf-8")
    guard = source.split("# ── Clean up print_queue: drop legacy columns ──")[1].split("if has_legacy:")[0]
    sentinels = set(re.findall(r'column_exists\(\s*conn,\s*"print_queue",\s*"(\w+)"', guard))

    assert sentinels, "m002's print_queue guard no longer reads as column_exists checks - re-read it"

    # The checks are ANDed, so one column the model cannot have is enough to keep
    # a fresh schema out of this branch. Requiring every sentinel to be dead would
    # be wrong: `require_previous_success` is a legitimate half of the pair, and
    # m116 legitimately brought it back.
    assert " or " not in guard, (
        "m002's legacy guard is ORed - a fresh schema matching any one sentinel takes the legacy path. "
        "The checks must be ANDed."
    )

    live = set(PrintQueueItem.__table__.columns.keys())
    dead = sentinels - live
    assert dead, (
        f"m002 detects the legacy print_queue only by {sorted(sentinels)}, and the current model declares "
        f"all of them - every fresh install will take the legacy path and fail to boot. At least one "
        f"sentinel must be a column m002 removes and nothing restores."
    )
