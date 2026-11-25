"""P-ID T5: SQLite (WAL) → ZIP backup → the real restore → restart: the printer counter comes back with the copy.

The backup holds printers 1–2 and a counter of 3 (printer 3 was deleted before it was taken). After the
restore the next printer is 4: the copy carried ``sqlite_sequence``, where ``max(id) + 1`` would be 3. The
live database had already reached 4 before the restore; restoring an older copy brings its own counter
back — the documented limit of the guarantee (spec §E).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.integration


def _run(mode: str, tmp_path: Path) -> dict:
    env = {**os.environ, "DATA_DIR": str(tmp_path / "data"), "DEBUG": "false", "BAMDUDE_IGNORE_DOTENV": "1"}
    env.pop("MFA_ENCRYPTION_KEY", None)
    env.pop("DATABASE_URL", None)
    (tmp_path / "out").mkdir(exist_ok=True)
    process = subprocess.run(
        [sys.executable, "-m", "backend.tests.integration.printer_id_zip_runner", mode, str(tmp_path / "out")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    assert process.returncode == 0, process.stdout[-3000:] + process.stderr[-8000:]
    return json.loads(process.stdout.strip().splitlines()[-1])


def test_the_zip_backup_brings_the_printer_counter_back_through_a_restore(tmp_path):
    backup = _run("backup", tmp_path)
    assert backup == {"ids": [1, 2, 3], "later": 4, "journal_mode": "wal", "restored": True}
    after = _run("after", tmp_path)
    assert after == {"rows": "1,2", "counter": 3, "autoincrement": True, "next": 4}
