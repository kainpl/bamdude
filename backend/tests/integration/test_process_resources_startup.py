"""Linux acceptance runs only in fresh owned children, never the pytest worker."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux descriptor/process acceptance")
ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("mode", ["startup", "fd", "sticky"])
def test_resource_contract_in_owned_child(tmp_path, mode):
    env = {
        **os.environ,
        "DATA_DIR": str(tmp_path),
        "LOG_DIR": str(tmp_path),
        "LOG_TO_FILE": "false",
        "BAMDUDE_IGNORE_DOTENV": "1",
        # This child owns a disposable SQLite DB; never inherit a live URL.
        "DATABASE_URL": "",
        "BAMDUDE_GC_STARTUP_FREEZE": "true",
    }
    done = subprocess.run(
        [sys.executable, "-m", "backend.tests.integration.process_resources_child", mode],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-6000:]
    result = json.loads(next(line[7:] for line in done.stdout.splitlines() if line.startswith("RESULT=")))
    if mode == "startup":
        assert result["marks"] == ["heap", "limits", "child"]
    elif mode == "sticky":
        assert result["old_client_collected"] and result["old_socketpair_released"]
        assert result["new_client_connected"] and result["old_queue_not_replayed"]
    else:
        assert result["exhausted"]["count"] is None
