"""The Windows installer workflow (spec stage B R6, R13; plan Task 7): only a pushed tag publishes, only after the
FULL acceptance of the very artifact it publishes, never a candidate pin; the acceptance runs every scenario on a
real Windows runner without secrets and without a window nobody can answer."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_TEXT = (ROOT / ".github" / "workflows" / "windows-installer.yml").read_text(encoding="utf-8")
WORKFLOW = yaml.safe_load(WORKFLOW_TEXT)
JOBS = WORKFLOW["jobs"]
ACCEPT = (ROOT / "scripts" / "windows_installer_acceptance.ps1").read_text(encoding="utf-8")
CHECK = ROOT / "scripts" / "check_release_payload.py"


def _runs(job: str) -> str:
    return "\n".join(str(step.get("run", "")) + str(step.get("uses", "")) for step in JOBS[job].get("steps", []))


def test_only_a_pushed_tag_publishes_and_only_after_full_acceptance():
    release = JOBS["release"]
    # The accepted installer, and the verified STEP sources of this tag (test_step_preview_sources.py pins that job).
    assert sorted(release["needs"]) == ["acceptance", "sources"]
    condition = release["if"]
    assert "github.event_name == 'push'" in condition
    assert "startsWith(github.ref, 'refs/tags/v')" in condition
    assert "needs.acceptance.outputs.full == 'true'" in condition
    assert JOBS["acceptance"]["needs"] in ("build", ["build"])
    assert "softprops/action-gh-release" in _runs("release")
    assert all("softprops/action-gh-release" not in _runs(job) for job in JOBS if job != "release")


def test_a_tag_never_builds_or_publishes_a_candidate():
    inputs = (WORKFLOW.get("on") or WORKFLOW.get(True))["workflow_dispatch"]["inputs"]
    assert inputs["step_preview_candidate"]["default"] is False
    stage = next(s for s in JOBS["build"]["steps"] if "build.py" in str(s.get("run", "")))
    assert stage["env"]["CANDIDATE"] == "${{ github.ref_type != 'tag' && inputs.step_preview_candidate }}"
    assert "$env:CANDIDATE -eq 'true'" in stage["run"]
    assert WORKFLOW_TEXT.count("--allow-candidate-cad") == 1  # nowhere else
    assert "check_release_payload.py check" in _runs("release")


def test_the_release_publishes_the_accepted_artifact_without_rebuilding():
    runs = _runs("release")
    assert "build.py" not in runs and "ISCC" not in runs
    assert "actions/download-artifact" in runs


def test_the_full_acceptance_runs_every_scenario():
    scenarios = ["S1", "S2", "S3", "S4", "S4b", "S5", "S8", "S6", "S6b", "S7", "S0"]
    for name in scenarios:
        assert re.search(rf"^function {name} \{{", ACCEPT, re.M), name
    full = re.search(r"\$Full = @\(([^)]*)\)", ACCEPT).group(1)
    assert [n.strip().strip("'") for n in full.split(",")] == scenarios
    starts = re.findall(r"Start-Process[^\n]*", ACCEPT)
    assert starts and all("-PassThru" in s for s in starts)
    setup = re.search(r"^function Invoke-Setup \{.*?^\}", ACCEPT, re.M | re.S).group(0)
    for flag in ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG="):
        assert flag in setup, flag
    assert "WaitForExit" in setup and "taskkill" in setup  # a deadline, and the whole tree ended past it
    assert "53d4579e114a2f1622dc702118e91344503d929eb80b57fd30513414940563a2" in ACCEPT  # v0.7.0, the exact asset


def test_check_release_payload_refuses_candidate_or_another_exe(tmp_path):
    exe = tmp_path / "setup.exe"
    exe.write_bytes(b"MZ installer bytes")
    sha = hashlib.sha256(exe.read_bytes()).hexdigest()

    def check(payload: dict, acceptance: dict) -> int:
        (tmp_path / "payload.json").write_text(json.dumps(payload), encoding="utf-8")
        (tmp_path / "acceptance.json").write_text(json.dumps(acceptance), encoding="utf-8")
        cmd = [
            sys.executable,
            str(CHECK),
            "check",
            "--exe",
            str(exe),
            "--payload",
            str(tmp_path / "payload.json"),
            "--acceptance",
            str(tmp_path / "acceptance.json"),
        ]
        return subprocess.run(cmd, capture_output=True, timeout=60).returncode

    good_payload = {"sha256": sha, "receipt_status": "release"}
    good = {"sha256": sha, "installed_receipt_status": "release", "full": True, "scenarios": {"S1": "passed"}}
    assert check(good_payload, good) == 0
    assert check({**good_payload, "receipt_status": "candidate"}, good) == 1
    assert check(good_payload, {**good, "installed_receipt_status": "candidate"}) == 1
    assert check(good_payload, {**good, "sha256": "0" * 64}) == 1
    assert check({**good_payload, "sha256": "0" * 64}, good) == 1
    assert check(good_payload, {**good, "full": False}) == 1
    assert check(good_payload, {**good, "scenarios": {"S1": "failed: x"}}) == 1


def test_the_acceptance_holds_no_secrets():
    assert "secrets." not in WORKFLOW_TEXT
    assert WORKFLOW["permissions"] == {"contents": "read"}
    for job in ("build", "acceptance"):
        assert JOBS[job]["permissions"] == {"contents": "read"}, job
    assert JOBS["release"]["permissions"] == {"contents": "write"}


def _step(job: str, predicate):
    return next(step for step in JOBS[job]["steps"] if predicate(step))


def test_the_release_reads_the_acceptance_report_where_the_acceptance_puts_it():
    """A multi-path upload roots the artifact at the paths' common ancestor (two drives here: the workspace, the
    runner's temp and ProgramData), so the report is its own single-file artifact and lands at its root (final review C2)."""
    report = _step(
        "acceptance", lambda s: "upload-artifact" in str(s.get("uses")) and "acceptance.json" in str(s["with"]["path"])
    )
    assert report["with"]["path"].strip() == "installer/acceptance.json"
    name = report["with"]["name"]
    logs = _step("acceptance", lambda s: "upload-artifact" in str(s.get("uses")) and s["with"]["name"] != name)
    assert "acceptance.json" not in str(logs["with"]["path"])
    download = _step("release", lambda s: "download-artifact" in str(s.get("uses")) and s["with"]["name"] == name)
    folder = download["with"]["path"]
    check = _step("release", lambda s: "check_release_payload.py check" in str(s.get("run", "")))
    assert f"--acceptance {folder}/acceptance.json" in check["run"]


def test_the_release_publishes_exactly_one_versioned_installer():
    check = _step("release", lambda s: "check_release_payload.py check" in str(s.get("run", "")))
    assert '"${#exes[@]}" -eq 1' in check["run"]


def test_the_acceptance_keeps_every_log_and_never_waits_unbounded():
    reset = re.search(r"^function Reset-Machine \{.*?^\}", ACCEPT, re.M | re.S).group(0)
    assert reset.index("Save-Logs") < reset.index("Remove-Item")  # S1..S8's logs survive S0's reset
    uninstall = re.search(r"^function Invoke-Uninstall\(.*?^\}", ACCEPT, re.M | re.S).group(0)
    assert "taskkill" in uninstall or "Stop-Process" in uninstall  # its deadline ends the uninstaller too
    assert "could not be opened (error 5)" in re.search(r"^function S4b \{.*?^\}", ACCEPT, re.M | re.S).group(0)
    assert not re.search(r"\(Svc [A-Za-z]+\)\.Status", ACCEPT)  # strict mode: a missing service says so itself
    logs = _step("acceptance", lambda s: "upload-artifact" in str(s.get("uses")) and "logs" in str(s["with"]["path"]))
    assert "acceptance-logs" in str(logs["with"]["path"])


def test_the_build_describes_the_installer_size(tmp_path):
    exe = tmp_path / "setup.exe"
    exe.write_bytes(b"MZ" * 100)
    receipt = tmp_path / "app" / "runtime" / "step-preview" / "win-x64-cp312" / ("a" * 32) / "receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"status": "candidate"}), encoding="utf-8")
    version = tmp_path / "version.iss"
    version.write_text('#define MyAppVersion "0"\n#define StepMaxAppDirLen 109\n', encoding="utf-8")
    out = tmp_path / "payload.json"
    cmd = [
        sys.executable,
        str(CHECK),
        "describe",
        "--exe",
        str(exe),
        "--app",
        str(tmp_path / "app"),
        "--version-iss",
        str(version),
        "--out",
        str(out),
    ]
    assert subprocess.run(cmd, capture_output=True, timeout=60).returncode == 0
    answer = json.loads(out.read_text(encoding="utf-8"))
    assert answer["size_bytes"] == 200 and answer["receipt_status"] == "candidate" and answer["app_dir_limit"] == 109


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("pwsh") is None, reason="named skip: the harness runs in pwsh on Windows"
)
def test_every_digest_count_holds_under_strict_mode_with_one_digest(tmp_path):
    """Run 39131019056: S2 and S3 passed their installs and failed on their own last check — the script's strict mode
    refuses .Count on a function's one-element answer, which PowerShell unrolls to a string. Every assertion that
    counts digests, run as the script runs it, on a tree with exactly one digest."""
    functions = [
        re.search(r"^function Bundle-Root\b.*$", ACCEPT, re.M).group(0),
        re.search(r"^function Digests\b.*?^\}", ACCEPT, re.M | re.S).group(0),
    ]
    checks = re.findall(r"Assert \((.*Digests.*)\) '", ACCEPT)
    assert len(checks) >= 2, checks
    (tmp_path / "app" / "runtime" / "step-preview" / "k" / ("a" * 32)).mkdir(parents=True)
    head = ["$ErrorActionPreference = 'Stop'", "Set-StrictMode -Version 3", "$Key = 'k'", f"$AppDir = '{tmp_path}'"]
    body = [f"if (-not ({check})) {{ throw 'check {i} is false' }}" for i, check in enumerate(checks)]
    script = tmp_path / "probe.ps1"
    script.write_text("\n".join([*head, *functions, *body, "'all held'"]), encoding="utf-8")
    cmd = ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(script)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0 and "all held" in p.stdout, (p.stdout, p.stderr)


def test_only_a_dispatch_that_asks_for_it_skips_the_ci_gate():
    """The owner's call (2026-10-18): a dispatched acceptance may run without a green CI run of its SHA, on an
    explicit input that defaults off. A tag push is gated as before, and the release still needs a pushed tag."""
    skip = WORKFLOW[True]["workflow_dispatch"]["inputs"]["skip_ci_gate"]  # yaml reads `on` as True
    assert skip["type"] == "boolean" and skip["default"] is False
    gate_if = JOBS["gate"]["if"]
    assert "github.event_name != 'workflow_dispatch'" in gate_if and "inputs.skip_ci_gate" in gate_if
    build_if = JOBS["build"]["if"]
    assert "needs.gate.result == 'success'" in build_if
    assert "needs.gate.result == 'skipped'" in build_if and "github.event_name == 'workflow_dispatch'" in build_if
    assert "inputs.skip_ci_gate" in build_if and "!cancelled()" in build_if
    # A skipped gate up the chain would skip every job after build too unless each says what it needs.
    assert "needs.build.result == 'success'" in JOBS["acceptance"]["if"]
    assert "github.event_name == 'push'" in JOBS["release"]["if"]


def test_a_bang_in_the_install_directory_is_refused_on_the_real_installer():
    """S6b: a directory with `!` is refused with Setup's 7, nothing created, the service untouched (owner, 2026-10-18)."""
    s6b = re.search(r"^function S6b \{.*?^\}", ACCEPT, re.M | re.S).group(0)
    assert "!" in re.search(r"^\$BangDir = (.*)$", ACCEPT, re.M).group(1)
    for check in ("$code -eq 7", "Test-Path -LiteralPath $BangDir", "Svc-Status BamDude"):
        assert check in s6b, check
    reset = re.search(r"^function Reset-Machine \{.*?^\}", ACCEPT, re.M | re.S).group(0)
    assert "$BangDir" in reset  # whatever a failed run left there goes with the reset


# --------------------------------------------------------------------------- #
# Release assets: only downloads, one checksum list (owner, 2026-11-04)
# --------------------------------------------------------------------------- #


def _attached() -> list[str]:
    attach = _step("release", lambda s: "softprops/action-gh-release" in str(s.get("uses", "")))
    return [line.strip() for line in attach["with"]["files"].splitlines() if line.strip()]


def test_the_release_attaches_the_client_packages_by_name_and_no_build_files():
    """The AI build directory also holds the server's own catalog (packages.json) and that build's partial
    SHA256SUMS. Neither is a download: the release names exactly the packages the server offers."""
    from backend.app.mcp_server.packages import PACKAGE_FILES

    files = _attached()
    assert "ai-packages/*" not in files
    assert sorted(f.removeprefix("ai-packages/") for f in files if f.startswith("ai-packages/")) == sorted(
        PACKAGE_FILES.values()
    )
    assert "checksums/*" in files


def _checksum_step() -> str:
    return _step("release", lambda s: s.get("name") == "Write release checksums")["run"]


def _posix_bash() -> str | None:
    """A bash that runs on this tree's paths. On Windows that is Git's: System32's bash.exe is the WSL launcher,
    which runs nothing of this checkout."""
    import os

    found = shutil.which("bash")
    if not found or os.name != "nt" or "system32" not in found.lower():
        return found
    git = shutil.which("git")
    for parent in Path(git).resolve().parents if git else ():
        candidate = parent / "bin" / "bash.exe"
        if candidate.exists():
            return str(candidate)
    return None


def test_the_release_checksums_name_every_package_the_server_offers():
    from backend.app.mcp_server.packages import PACKAGE_FILES

    run = _checksum_step()
    for name in PACKAGE_FILES.values():
        assert name in run, name
    names = [s.get("name") for s in JOBS["release"]["steps"]]
    # After the alias exists, before anything is attached.
    assert (
        names.index("Create unversioned alias")
        < names.index("Write release checksums")
        < names.index("Attach installer to release")
    )


@pytest.mark.skipif(_posix_bash() is None, reason="needs a POSIX bash with sha256sum")
@pytest.mark.parametrize("tampered", [False, True], ids=["as-built", "package-changed-after-its-build"])
def test_the_release_checksums_cover_this_jobs_downloads_and_only_vouch_for_built_packages(tmp_path, tampered):
    import os

    from backend.app.mcp_server.packages import PACKAGE_FILES

    installer, packages = tmp_path / "installer", tmp_path / "ai-packages"
    installer.mkdir()
    packages.mkdir()
    for name in ("bamdude-9.9.9-windows-x64-setup.exe", "bamdude-windows-x64-setup.exe"):
        (installer / name).write_bytes(b"installer")
    (installer / "payload.json").write_text("{}")
    sums = []
    for name in sorted(PACKAGE_FILES.values()):
        (packages / name).write_bytes(name.encode())
        sums.append(f"{hashlib.sha256(name.encode()).hexdigest()}  {name}\n")
    (packages / "SHA256SUMS").write_bytes("".join(sums).encode())
    (packages / "packages.json").write_text("{}")
    if tampered:
        (packages / sorted(PACKAGE_FILES.values())[0]).write_bytes(b"changed after the build")

    proc = subprocess.run(
        [_posix_bash(), "-e", "-c", _checksum_step()],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_REF_NAME": "v9.9.9"},
        capture_output=True,
        text=True,
    )
    out = tmp_path / "checksums" / "bamdude-9.9.9-SHA256SUMS"
    if tampered:
        # Refused by the check against the build's own hashes -- not by anything else failing.
        assert proc.returncode != 0 and not out.exists(), proc.stdout + proc.stderr
        assert "FAILED" in proc.stdout + proc.stderr, proc.stdout + proc.stderr
        return
    assert proc.returncode == 0, proc.stdout + proc.stderr
    raw = out.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw
    listed = {}
    for line in raw.decode().splitlines():
        digest, name = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line).groups()
        listed[name] = digest
    expected = {"bamdude-9.9.9-windows-x64-setup.exe", "bamdude-windows-x64-setup.exe", *PACKAGE_FILES.values()}
    assert set(listed) == expected
    for name, digest in listed.items():
        folder = installer if name.endswith(".exe") else packages
        assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == digest, name
