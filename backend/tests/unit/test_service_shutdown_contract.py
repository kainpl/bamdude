"""Render installer templates without touching host services or configuration."""

import json
import os
import plistlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def bash_binary():
    if os.name == "nt":
        git = shutil.which("git")
        candidate = Path(git).resolve().parents[1] / "bin/bash.exe" if git else None
        if candidate and candidate.exists():
            return str(candidate)
        pytest.skip("Git Bash is required; do not use the Windows WSL launcher")
    return shutil.which("bash") or pytest.skip("bash not installed")


@pytest.mark.parametrize("mode", ["sqlite", "embedded", "external"])
@pytest.mark.parametrize("manager", ["systemd", "launchd"])
def test_installer_renders_child_shutdown_policy_for_every_db(tmp_path, mode, manager):
    script = (ROOT / "install/install.sh").read_text(encoding="utf-8")
    function = re.search(rf"^create_{manager}_service\(\) \{{\n.*?^\}}", script, re.M | re.S).group()
    output = tmp_path / "generated-service"
    # Only filesystem targets are redirected; execute the actual conditions,
    # variables and heredoc. No sudo, service changes or global /tmp writes.
    function = function.replace("/etc/systemd/system/bamdude.service", (tmp_path / "absent-unit").as_posix())
    function = function.replace("/tmp/bamdude.service", output.as_posix())
    function = function.replace("$HOME/Library/LaunchAgents/com.bamdude.app.plist", output.as_posix())
    setup = f"""
set -eu
OS_TYPE={"macos" if manager == "launchd" else "linux"}
DB_MODE={mode}
SKIP_SERVICE=false
INSTALL_PATH=/opt/bamdude
DATA_DIR=/data
LOG_DIR=/logs
SERVICE_USER=bamdude
TIMEZONE=UTC
BIND_ADDRESS=127.0.0.1
PORT=8000
DEBUG_MODE=false
LOG_LEVEL=INFO
log_info() {{ :; }}
log_success() {{ :; }}
prompt_yes_no() {{ return 1; }}
sudo() {{ return 0; }}
"""
    result = subprocess.run(
        [bash_binary(), "--noprofile", "--norc"],
        input=setup + function + f"\ncreate_{manager}_service\n",
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    if manager == "systemd":
        unit = output.read_text()
        assert "\nKillMode=mixed\n" in unit
        assert "\nTimeoutStopSec=90\n" in unit
        assert unit.count("TimeoutStopSec=") == 1
        assert "--loop asyncio --timeout-graceful-shutdown 15" in unit
    else:
        plist = plistlib.loads(output.read_bytes())
        assert plist["ExitTimeOut"] == 90
        assert not plist.get("AbandonProcessGroup", False)
        assert plist["ProgramArguments"][-4:] == ["--loop", "asyncio", "--timeout-graceful-shutdown", "15"]


def test_manual_systemd_template_matches_installer():
    unit = (ROOT / "deploy/bamdude.service").read_text(encoding="utf-8")
    assert "\nKillMode=mixed\n" in unit
    assert "\nTimeoutStopSec=90\n" in unit
    assert "--loop asyncio --timeout-graceful-shutdown 15" in unit


def test_nssm_grace_is_unconditional_and_precedes_upgrade_stop():
    script = (ROOT / "installers/windows/service/install-service.bat").read_text(encoding="utf-8")
    setting = '"%NSSM%" set BamDude AppStopMethodConsole 90000'
    assert script.index(setting) < script.index('"%NSSM%" stop BamDude')
    # After service recreation the setting must be top-level, not a DB branch.
    block = script.split('"%NSSM%" set BamDude AppEnvironmentExtra', 1)[1].split("REM embedded-service:", 1)[0]
    assert f"\n{setting}\n" in block
    assert not re.search(r"^\s*if\b", block, re.M | re.I)
    assert "--loop asyncio --timeout-graceful-shutdown 15" in script
    uninstall = (ROOT / "installers/windows/service/uninstall-service.bat").read_text(encoding="utf-8")
    assert uninstall.index(setting) < uninstall.index('"%NSSM%" stop BamDude')
    installer = (ROOT / "installers/windows/bamdude.iss").read_text(encoding="utf-8")
    prepare = installer.split("function PrepareToInstall", 1)[1].split("\nend;", 1)[0]
    assert prepare.index("set BamDude AppStopMethodConsole 90000") < prepare.index("StopServiceAndWait('BamDude'")
    assert "StopServiceAndWait('BamDude', 100)" in prepare


def test_docker_expansion_shell_execs_application():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    command = json.loads(re.search(r"^CMD (\[.*\])$", dockerfile, re.M).group(1))
    assert command[:2] == ["sh", "-c"]
    assert command[2].startswith("exec uvicorn ")
    assert "--loop asyncio --timeout-graceful-shutdown 15" in command[2]
    assert "stop_grace_period: 60s" in (ROOT / "docker-compose.yml").read_text(encoding="utf-8")


def test_ci_preview_smoke_is_one_shell_command():
    import shlex

    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    step = next(
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if step.get("name") == "Exercise local preview broker, service and renderer"
    )
    # More-indented lines in folded YAML retain newlines, making the shell
    # execute the second test path as a program rather than a pytest argument.
    assert len(step["run"].strip().splitlines()) == 1
    args = shlex.split(step["run"])
    assert args[:3] == ["python", "-m", "pytest"]
    assert "backend/tests/unit/test_service_shutdown_contract.py" in args
    assert args[-2:] == ["-n", "1"]
