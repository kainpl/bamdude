"""The native install acceptance (spec stage C R12; plan Task 9): a dispatch-only workflow, one job per scenario,
GitHub redirected to a local remote that serves the candidate for every git context, the installed code proven by its
HEAD and the service by its process, a finite "y" for update.sh's questions and the phases it must reach.

The workflow itself only runs on disposable runners; here its structure is read, and the harness's pure-git and
reporting halves run for real."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from backend.tests.unit.test_update_script_repairs_loop_flag import BASH

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "native-install-acceptance.yml"
SCRIPT = ROOT / "scripts" / "ci" / "native_install_acceptance.sh"
SCENARIOS = ["N1", "N2", "N3", "N4", "N5", "N6", "N7", "F1", "M1"]
LINUX = ["N1", "N2", "N3", "N4", "N5", "N6", "N7"]


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _function(name: str) -> str:
    """The body of one shell function of the harness, up to its closing brace at column 0."""
    match = re.search(rf"^{re.escape(name)}\(\) \{{[^\n]*\n(.*?)^\}}", _script(), re.S | re.M)
    assert match, f"no function {name} in the harness"
    return match.group(1)


def _scenario_jobs() -> dict[str, dict]:
    jobs = _workflow()["jobs"]
    found = {}
    for job in jobs.values():
        for step in job.get("steps", []):
            m = re.search(r"native_install_acceptance\.sh (N[1-7]|F1|M1)\b", step.get("run", ""))
            if m:
                found[m.group(1)] = job
    return found


def test_the_native_acceptance_runs_only_by_hand_without_secrets():
    wf = _workflow()
    triggers = wf.get("on", wf.get(True))  # YAML 1.1 reads a bare `on` as True
    assert set(triggers) == {"workflow_dispatch"}
    assert set(triggers["workflow_dispatch"]["inputs"]["scope"]["options"]) == {"full", "diagnostic"}
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in text and "GITHUB_TOKEN" not in text and "github.token" not in text
    assert wf["permissions"] == {"contents": "read"}
    for job in wf["jobs"].values():
        assert job.get("permissions", {"contents": "read"}) == {"contents": "read"}
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False


def test_every_scenario_is_its_own_job():
    jobs = _scenario_jobs()
    assert sorted(jobs) == sorted(SCENARIOS)
    assert len({id(job) for job in jobs.values()}) == len(SCENARIOS)
    for name, job in jobs.items():
        needs = job["needs"] if isinstance(job["needs"], list) else [job["needs"]]
        assert "fixture" in needs, name
        # Bounded, and above the sum of the scenario's own deadlines (test below); GitHub's own cap is 360.
        assert 0 < job["timeout-minutes"] <= 150, name
        always = [s for s in job["steps"] if s.get("if") == "always()"]
        assert any(f"native_install_acceptance.sh cleanup {name}" in s.get("run", "") for s in always), name
        assert any(
            s.get("with", {}).get("name") == f"native-acceptance-{name}"
            and s["with"]["path"].endswith(f"native-acceptance-{name}.json")
            for s in always
        ), name
    assert all(jobs[n]["runs-on"] == "ubuntu-latest" for n in LINUX)
    assert jobs["F1"]["container"]["image"] == "fedora:latest"
    assert jobs["M1"]["runs-on"] == "macos-latest"


_NOT_STEPS = {"log", "fail", "note", "logged", "bounded", "on_error", "json_string", "write_report", "main", "usage"}


def _deadline_seconds(name: str, seen: tuple[str, ...] = ()) -> int:
    """The longest a harness function can run by its own bounds: its `logged`/`bounded` deadlines and its wait loops,
    plus those of every harness function it calls, as many times as it calls them."""
    text = _script()
    consts = {k: int(v) for k, v in re.findall(r"^([A-Z_]+_SECONDS)=(\d+)$", text, re.M)}

    def seconds(token: str) -> int:
        return int(token) if token.isdigit() else consts.get(token, 0)

    body = _function(name)
    total = sum(seconds(t) for t in re.findall(r"\blogged\s+\S+\s+\"?\$?(\w+)\"?", body))
    total += sum(seconds(t) for t in re.findall(r"SECONDS \+ (\w+)\)", body))
    names = set(re.findall(r"^([a-z_][a-zA-Z0-9_]*)\(\) \{", text, re.M)) - _NOT_STEPS - {name, *seen}
    for callee in names:
        calls = len(re.findall(rf"(?<![\w/.-]){re.escape(callee)}(?=\s|$|\))", body))
        if calls:
            total += calls * _deadline_seconds(callee, (*seen, name))
    return total


def test_a_job_outlasts_the_deadlines_of_its_own_scenario():
    """A slow but bounded step must end by the harness's own `fail`, with its report line — never be cut by the
    runner, which loses it (shell review S9). Ten minutes on top cover the job's own setup steps."""
    for scenario, job in _scenario_jobs().items():
        need = _deadline_seconds(f"scenario_{scenario}") + 600
        assert job["timeout-minutes"] * 60 >= need, f"{scenario}: {job['timeout-minutes']} min < {need // 60} min"


def test_the_diagnostic_scope_runs_a_named_subset():
    jobs = _scenario_jobs()
    full_only = {n for n, job in jobs.items() if job.get("if") == "inputs.scope == 'full'"}
    assert full_only == set(SCENARIOS) - {"N1", "N2", "F1"}


def test_the_installed_code_is_proven_by_its_head():
    head = _function("expect_head")
    assert "rev-parse HEAD" in head and "fail" in head
    for name in SCENARIOS:
        body = _function(f"scenario_{name}")
        lines = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        for i, line in enumerate(lines):
            if re.match(r"(install_linux|install_macos|run_update|clone_as_ci)\b", line):
                assert lines[i + 1].startswith("expect_head "), f"{name}: no HEAD check after `{line}`"
    n3 = _function("scenario_N3")
    assert (
        n3.index('expect_head install "$BASELINE"')
        < n3.index("run_update")
        < n3.index('expect_head update "$CANDIDATE"')
    )


def test_the_service_is_the_one_installed():
    ours = _function("service_is_ours")
    assert "MainPID" in ours
    assert "/proc/$pid/cwd" in ours and "INSTALL_PATH" in ours
    assert "sport = :$PORT" in ours and "pid=$pid," in ours
    for name in LINUX:
        body = _function(f"scenario_{name}")
        assert "service_is_ours" in body, name
        if "http " in body:
            assert body.index("service_is_ours") < body.index("http "), name


def test_github_is_redirected_to_the_fixture_for_root_too():
    setup = _function("fixture_setup_linux")
    assert re.search(r"sudo git config --system url\.\"file://\$FIXTURE\"\.insteadOf \"\$REPO_URL\"", setup)
    assert re.search(r"sudo git config --system --add safe\.directory \"\$FIXTURE\"", setup)
    assert re.search(r"\$\(git ls-remote --get-url \"\$REPO_URL\"\)", setup)
    assert re.search(r"sudo git ls-remote --get-url \"\$REPO_URL\"", setup)
    assert setup.rindex("--get-url") < setup.index("remote_main_is")  # offline first, then the real one
    real = _function("remote_main_is")
    assert '$(git ls-remote "$REPO_URL"' in real and 'sudo git ls-remote "$REPO_URL"' in real
    assert setup.index("sudo git config --system") < setup.index("--get-url")
    assert 'REPO_URL="https://github.com/kainpl/bamdude.git"' in _script()
    for name in LINUX:
        body = _function(f"scenario_{name}")
        assert body.index("fixture_setup_linux") < body.index("install_linux"), name
    mac = _function("fixture_setup_macos")
    assert "git config --global url." in mac and "--get-url" in mac


def test_update_prompts_get_a_finite_yes_and_phases_are_checked():
    text = _script()
    assert not re.search(r"\byes\s*\|", text)
    update = _function("run_update")
    outside = text.replace(update, "")
    for line in outside.splitlines():
        code = line.split("#", 1)[0]
        if re.search(r"update\.sh", code):  # every other mention passes the script to run_update, never runs it
            assert re.match(r"\s*run_update ", code), line
    assert re.search(r"printf 'y\\ny\\n' \| logged update \"\$UPDATE_SECONDS\"", update)
    assert "bounded" in _function("logged") and "timeout" in _function("bounded")
    phases = _function("update_phases")
    for phase in ("Stopping service: bamdude", "Updating the STEP preview component", "Starting service: bamdude"):
        assert phase in phases
    for name in ("N3", "N4", "N7"):
        body = _function(f"scenario_{name}")
        after = body[body.index("run_update") :]
        want = "stop cad prune start" if name in ("N4", "N7") else "stop cad start"
        assert f"update_phases update {want}" in after, name


def _bash():
    if not shutil.which("git") or not BASH:
        pytest.skip("named skip: git or bash is not available")
    return BASH


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_the_fixture_serves_the_candidate_and_the_previous_release(tmp_path):
    bash = _bash()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for n, tag in ((1, "v0.6.1"), (2, "v0.7.0b1"), (3, "v0.7.0"), (4, None)):
        (repo / "f.txt").write_text(str(n), encoding="utf-8")
        _git(repo, "add", "f.txt")
        _git(repo, "-c", "user.email=a@b", "-c", "user.name=n", "commit", "-q", "-m", f"c{n}")
        if tag:
            _git(repo, "tag", tag)
    candidate = _git(repo, "rev-parse", "HEAD")
    out = tmp_path / "fixture"
    result = subprocess.run(
        [bash, SCRIPT.as_posix(), "fixture", out.as_posix(), candidate],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    meta = json.loads((out / "fixture.json").read_text(encoding="utf-8"))
    assert meta == {
        "schema": 1,
        "candidate": candidate,
        "baseline": _git(repo, "rev-parse", "v0.7.0^{commit}"),
        "baseline_tag": "v0.7.0",
    }
    unpacked = tmp_path / "unpacked"
    unpacked.mkdir()
    subprocess.run(["tar", "-xf", "fixture/bamdude.git.tar", "-C", "unpacked"], cwd=tmp_path, check=True)
    heads = _git(unpacked, "--git-dir", "bamdude.git", "for-each-ref", "--format=%(refname) %(objectname)")
    assert heads.splitlines() == [
        f"refs/heads/baseline {meta['baseline']}",
        f"refs/heads/main {candidate}",
    ]


def test_the_fixture_starts_no_maintenance_beside_its_tar(tmp_path):
    """A fetch starts `git maintenance run --auto`, which detaches on a current git: tar then read the bare remote
    while it took `objects/maintenance.lock` and failed with "file changed as we read it" (run 39300867991)."""
    bash = _bash()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for n, tag in ((1, "v0.7.0"), (2, None)):
        (repo / "f.txt").write_text(str(n), encoding="utf-8")
        _git(repo, "add", "f.txt")
        _git(repo, "-c", "user.email=a@b", "-c", "user.name=n", "commit", "-q", "-m", f"c{n}")
        if tag:
            _git(repo, "tag", tag)
    trace = tmp_path / "trace2.json"
    result = subprocess.run(
        [bash, SCRIPT.as_posix(), "fixture", (tmp_path / "fixture").as_posix(), _git(repo, "rev-parse", "HEAD")],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "GIT_TRACE2_EVENT": trace.as_posix()},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    children = [
        event["argv"]
        for event in map(json.loads, trace.read_text(encoding="utf-8").splitlines())
        if event.get("event") == "child_start" and event.get("argv")
    ]
    assert children, "the trace saw no child process at all"
    assert not [argv for argv in children if {"maintenance", "gc"} & set(argv[1:2])], children


def test_the_fixture_lets_an_install_of_the_baseline_update_to_the_candidate(tmp_path):
    """N3's path with the real fixture and plain git, as install.sh and update.sh use it (Codex C-I-R3): clone main
    while it is the baseline, move main to the candidate, an ordinary fetch, a reset. Shallow roots made that fetch
    keep the old SHA — with exit 0."""
    bash = _bash()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for n, tag in ((1, None), (2, "v0.7.0"), (3, None), (4, None)):
        (repo / "f.txt").write_text(str(n), encoding="utf-8")
        _git(repo, "add", "f.txt")
        _git(repo, "-c", "user.email=a@b", "-c", "user.name=n", "commit", "-q", "-m", f"c{n}")
        if tag:
            _git(repo, "tag", tag)
    candidate = _git(repo, "rev-parse", "HEAD")
    baseline = _git(repo, "rev-parse", "v0.7.0^{commit}")
    out = tmp_path / "fixture"
    made = subprocess.run(
        [bash, SCRIPT.as_posix(), "fixture", out.as_posix(), candidate], cwd=repo, capture_output=True
    )
    assert made.returncode == 0, made.stderr
    subprocess.run(["tar", "-xf", "bamdude.git.tar"], cwd=out, check=True)
    bare = out / "bamdude.git"
    _git(out, "--git-dir", "bamdude.git", "update-ref", "refs/heads/main", baseline)
    _git(tmp_path, "clone", "-q", "--branch", "main", bare.as_uri(), "installed")
    installed = tmp_path / "installed"
    assert _git(installed, "rev-parse", "HEAD") == baseline
    _git(out, "--git-dir", "bamdude.git", "update-ref", "refs/heads/main", candidate)
    _git(installed, "fetch", "--prune", "origin")
    assert _git(installed, "rev-parse", "origin/main") == candidate
    _git(installed, "reset", "-q", "--hard", "origin/main")
    assert _git(installed, "rev-parse", "HEAD") == candidate


def test_a_failed_scenario_still_writes_its_report(tmp_path):
    bash = _bash()
    reports = tmp_path / "reports"
    result = subprocess.run(
        [bash, SCRIPT.as_posix(), "N1", (tmp_path / "no-fixture").as_posix()],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "REPORT_DIR": reports.as_posix(), "RUNNER_TEMP": tmp_path.as_posix()},
    )
    assert result.returncode != 0
    report = json.loads((reports / "native-acceptance-N1.json").read_text(encoding="utf-8"))
    assert report["scenario"] == "N1" and report["result"] == "failed"
    assert "fixture" in report["failure"]


def _sourced(tmp_path: Path, steps: str) -> tuple[int, str]:
    """Run steps against the harness's own functions, sourced under its own options."""
    program = "\n".join(
        [
            f"export REPORT_DIR='{(tmp_path / 'reports').as_posix()}' RUNNER_TEMP='{tmp_path.as_posix()}'",
            f"source '{SCRIPT.as_posix()}'",
            "rc=0",
            f"( {steps} ) || rc=$?",
            'echo "RC=$rc"',
        ]
    )
    script = tmp_path / "steps.sh"
    script.write_text(program, encoding="utf-8", newline="\n")
    result = subprocess.run([_bash(), script.as_posix()], capture_output=True, text=True, timeout=60)
    rc = re.search(r"RC=(\d+)", result.stdout)
    assert rc, result.stdout + result.stderr
    return int(rc.group(1)), result.stdout + result.stderr


UPDATE_LOG = """[bamdude-update] Stopping service: bamdude
[bamdude-update] Updating the STEP preview component (if enabled)
[bamdude-update] Starting service: bamdude
[bamdude-update] Update complete: aaaaaaa -> bbbbbbb
"""


@pytest.mark.parametrize(
    ("log", "phases", "missing"),
    [
        (UPDATE_LOG, "stop cad prune start", None),
        (UPDATE_LOG.replace("[bamdude-update] Starting service: bamdude\n", ""), "stop cad start", "start"),
        (UPDATE_LOG.replace("(if enabled)", ""), "stop cad start", "cad"),
        (
            UPDATE_LOG + "[WARN] The old STEP preview component was kept: the stop could not be proven\n",
            "prune",
            "prune",
        ),
    ],
)
def test_update_phases_refuses_a_log_that_missed_one(tmp_path, log, phases, missing):
    logs = tmp_path / "reports" / "logs"
    logs.mkdir(parents=True)
    (logs / "update.log").write_text(log, encoding="utf-8", newline="\n")
    rc, out = _sourced(tmp_path, f"update_phases update {phases}")
    if missing is None:
        assert rc == 0, out
    else:
        assert rc == 1 and f"did not reach its {missing} phase" in out, out


def test_expect_head_refuses_another_commit(tmp_path):
    _bash()  # the named skip before any git: an image without git skips, never errors
    repo = tmp_path / "install"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=a@b", "-c", "user.name=n", "commit", "-q", "--allow-empty", "-m", "x")
    head = _git(repo, "rev-parse", "HEAD")
    rc, out = _sourced(tmp_path, f"INSTALL_PATH='{repo.as_posix()}'; expect_head install {head}; echo \"$HEADS\"")
    assert rc == 0 and f'"expected": "{head}", "actual": "{head}"' in out, out
    rc, out = _sourced(tmp_path, f"INSTALL_PATH='{repo.as_posix()}'; expect_head install {'0' * 40}")
    assert rc == 1 and f"HEAD is {head}, expected {'0' * 40}" in out, out


def test_nothing_hands_a_shell_function_to_the_deadline():
    """`bounded` runs GNU timeout, an external program: a harness function given to it is "No such file or
    directory", exit 127 (Codex C-I-R2: F1's venv and helper steps). Every bounded command is an executable."""
    text = _script()
    functions = set(re.findall(r"^(\w+)\(\) \{", text, re.M))
    for line in text.splitlines():
        code = line.split("#", 1)[0]
        m = re.search(r"\blogged\s+\S+\s+\S+\s+(.+)", code) or re.search(r"\bbounded\s+\S+\s+(.+)", code)
        if not m or code.strip().startswith(("logged()", "bounded()")):
            continue
        words = [w for w in m.group(1).split() if not w.startswith("${OWNER_PREFIX")]
        if words and words[0] not in ('"$@"', "\\"):
            assert words[0].strip("\"'") not in functions, line


def _owner_steps(tmp_path: Path, extra: str = "") -> tuple[int, str, list[str]]:
    install = tmp_path / "inst dir"
    (install / "venv" / "bin").mkdir(parents=True)
    calls = tmp_path / "calls.txt"
    calls.write_text("", encoding="utf-8")
    recorder = (
        "#!/usr/bin/env bash\n"
        'printf "%s cwd=%s" "$(basename "$0")" "$(basename "$PWD")" >> "$CALLS"\n'
        'for a in "$@"; do printf " [%s]" "$a" >> "$CALLS"; done\n'
        'echo >> "$CALLS"\n'
        'if [[ -n "${FAKE_SLEEP:-}" && "$(basename "$0")" == python ]]; then exec sleep "$FAKE_SLEEP"; fi\n'
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for path in (install / "venv" / "bin" / "python", install / "venv" / "bin" / "pip", bindir / "python3.12"):
        path.write_text(recorder, encoding="utf-8", newline="\n")
        path.chmod(0o755)
    steps = (
        f"export CALLS='{calls.as_posix()}'; {extra}"
        f"OWNER_PREFIX=(); PYTHON='{(bindir / 'python3.12').as_posix()}'; INSTALL_PATH='{install.as_posix()}'; "
        "build_venv && install_component"
    )
    rc, out = _sourced(tmp_path, steps)
    return rc, out, calls.read_text(encoding="utf-8").splitlines()


def test_the_owner_steps_cross_the_deadline_as_commands(tmp_path):
    if not shutil.which("timeout"):
        pytest.skip("named skip: no GNU timeout here, bounded runs in-shell")
    rc, out, calls = _owner_steps(tmp_path)
    assert rc == 0, out
    assert calls == [
        "python3.12 cwd=inst dir [-m] [venv] [venv]",
        "pip cwd=inst dir [install] [-q] [-r] [requirements.txt]",
        f"python cwd=inst dir [-m] [backend.app.step_runtime] [install] [--app-dir] [{(tmp_path / 'inst dir').as_posix()}]",
    ], out


def test_an_owner_step_past_its_deadline_is_ended(tmp_path):
    if not shutil.which("timeout"):
        pytest.skip("named skip: no GNU timeout here, bounded runs in-shell")
    rc, out, _ = _owner_steps(tmp_path, "export FAKE_SLEEP=30; ENABLE_SECONDS=1; ")
    assert rc == 124 and "helper-install: exit 124" in out, out  # the helper step hit its deadline, and only it


def test_the_harness_parses_and_has_lf_endings():
    bash = _bash()
    assert SCRIPT.read_bytes().startswith(b"#!/usr/bin/env bash\n") and b"\r\n" not in SCRIPT.read_bytes()
    result = subprocess.run([bash, "-n", SCRIPT.as_posix()], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_macos_acceptance_selects_a_python_supported_by_the_cad_pin():
    steps = _scenario_jobs()["M1"]["steps"]
    setup = next((s for s in steps if s.get("uses", "").startswith("actions/setup-python@")), None)
    assert setup is not None, "the runner's default Python may be newer than the CAD wheels"
    assert str(setup["with"]["python-version"]) == "3.13"
    scenario = next(s for s in steps if "native_install_acceptance.sh M1 " in s.get("run", ""))
    assert steps.index(setup) < steps.index(scenario)


def test_controlled_app_observations_use_its_owner_in_an_unprivileged_container(tmp_path):
    # Container root lacks CAP_SYS_PTRACE: it can see LISTEN but not another uid's pid/cwd. The owner can.
    rc, out = _sourced(
        tmp_path,
        """OS=linux; PORT=8181
as_owner() { ROLE=owner "$@"; }
sudo() { ROLE=root "$@"; }
ss() { if [[ "${ROLE:-}" == owner ]]; then echo 'LISTEN users:(("uvicorn",pid=321,fd=9))';
       else echo 'LISTEN'; fi; }
readlink() { [[ "${ROLE:-}" == owner ]] && echo /home/ci/bamdude; }
echo "PID=$(listener)"
echo "CWD=$(process_cwd 321)"
""",
    )
    assert rc == 0 and "PID=321" in out and "CWD=/home/ci/bamdude" in out, out
