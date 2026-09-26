"""install/update.sh restores the ``--loop asyncio`` pin on an old unit file (upstream 0dfcff59).

install.sh has pinned the loop since 2026-07-08, but nothing rewrote an existing
unit file, so a native install created before that still ran on uvloop however
often it was updated. The update now inserts the one flag — and nothing else —
while the service is stopped, backing the file up first. A unit that is not a
plain single-line uvicorn command (a wrapper, a continuation, several ExecStart
lines, drop-ins that may define it) is described, never edited.

The function is lifted out of the script and run in bash against a stub
``systemctl``, so the test exercises the real shell code.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "install" / "update.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not available")

PLAIN = (
    "[Service]\n"
    "ExecStart=/opt/bamdude/venv/bin/uvicorn backend.app.main:app --host 0.0.0.0 --port 8000\n"
    "Restart=always\n"
)


def _function_source() -> str:
    text = SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
    match = re.search(r"^repair_loop_flag\(\) \{\n.*?^\}\n", text, re.S | re.M)
    assert match, "update.sh must define repair_loop_flag()"
    return match.group(0)


def _systemctl_stub(fragment: Path, drop_ins: str) -> str:
    """A shell FUNCTION, not a stub on PATH: it shadows the command the same way
    on Linux CI and under Git Bash, where a Windows PATH does not carry over."""
    path = fragment.as_posix()
    lines = [
        "systemctl() {",
        '  case "$*" in',
        f"    *--property=ExecStart*) grep '^ExecStart=' '{path}' || true ;;",
        f"    *--property=FragmentPath*) echo '{path}' ;;",
        f"    *--property=DropInPaths*) echo '{drop_ins}' ;;",
        "    *) true ;;",
        "  esac",
        "}",
    ]
    return "\n".join(lines) + "\n"


def _run(tmp_path: Path, unit: str, *, drop_ins: str = "") -> tuple[str, str]:
    fragment = tmp_path / "bamdude.service"
    fragment.write_text(unit, encoding="utf-8", newline="\n")
    program = "\n".join(
        [
            'log() { echo "LOG $*"; }',
            'warn() { echo "WARN $*"; }',
            "SERVICE_NAME=bamdude",
            _systemctl_stub(fragment, drop_ins),
            _function_source(),
            "repair_loop_flag",
        ]
    )
    # From a file, not ``bash -c``: Windows argv quoting eats the backslashes the
    # continuation check is made of.
    script = tmp_path / "run.sh"
    script.write_text(program, encoding="utf-8", newline="\n")
    result = subprocess.run([BASH, script.as_posix()], capture_output=True, text=True, timeout=30)
    return result.stdout, fragment.read_text(encoding="utf-8")


def test_a_plain_unit_gains_the_flag_and_nothing_else(tmp_path):
    out, after = _run(tmp_path, PLAIN)
    assert after == PLAIN.replace("--port 8000\n", "--port 8000 --loop asyncio\n")
    assert list(tmp_path.glob("bamdude.service.bak-*")), "the file is backed up first"
    assert "LOG" in out


def test_a_pinned_loop_is_left_alone(tmp_path):
    unit = PLAIN.replace("--port 8000", "--port 8000 --loop uvloop")
    _out, after = _run(tmp_path, unit)
    assert after == unit, "a deliberate --loop is the operator's"


@pytest.mark.parametrize(
    ("unit", "why"),
    [
        ("[Service]\nExecStart=/opt/bamdude/run.sh\n", "a wrapper script"),
        ("[Service]\nExecStart=/opt/bamdude/venv/bin/uvicorn backend.app.main:app \\\n  --port 8000\n", "continued"),
        (PLAIN + "ExecStart=/opt/bamdude/venv/bin/uvicorn other:app\n", "several ExecStart lines"),
    ],
)
def test_anything_but_a_plain_uvicorn_line_is_described_not_edited(tmp_path, unit, why):
    out, after = _run(tmp_path, unit)
    assert after == unit, why
    assert "WARN" in out


def test_drop_ins_are_not_second_guessed(tmp_path):
    out, after = _run(tmp_path, PLAIN, drop_ins="/etc/systemd/system/bamdude.service.d/override.conf")
    assert after == PLAIN
    assert "WARN" in out and "drop-in" in out
