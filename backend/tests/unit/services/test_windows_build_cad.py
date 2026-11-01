"""The Windows installer's CAD stage (spec stage B R4–R7, plan Task 3): build.py owns every call of the helper in the
embedded Python as one whole tree, with its own record written before any launch; installs into a build root of
its own, exports only the located bundle into the payload and verifies it there; refuses a candidate pin on any
tag; and derives the install directory limit from the catalog."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from backend.app import step_runtime as sr
from backend.app.services import part_render_tree as tree, step_runtime_child

ROOT = Path(__file__).resolve().parents[4]
BASE = getattr(sys, "_base_executable", sys.executable)
SLEEPY = ROOT / "backend" / "tests" / "fixtures" / "windows_build" / "sleepy_helper.py"
WIN312 = ["cp312-cp312-win_amd64", "cp312-abi3-win_amd64", "py3-none-win_amd64", "py3-none-any"]


@pytest.fixture
def tmp_path(install_tmp_path):
    return install_tmp_path  # an install root as short as a real one (see install_tmp_path)


@pytest.fixture
def build(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("bamdude_windows_build", ROOT / "installers" / "windows" / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, value in {
        "BUILD_DIR": tmp_path / "build",
        "STAGING": tmp_path / "build" / "staging",
        "DOWNLOADS": tmp_path / "build" / "downloads",
        "CAD_ROOT": tmp_path / "build" / "cad-build",
        "CAD_OWNER": tmp_path / "build" / "cad-owner",
    }.items():
        monkeypatch.setattr(module, name, value)
    return module


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _until(predicate, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


# --- order and the pin ------------------------------------------------------------------------------------------


def test_ai_packages_are_staged_beside_canonical_skill(build, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "backend").mkdir(parents=True)
    (repo / "backend" / "__init__.py").write_text("")
    skill = repo / "integrations" / "agent-skills" / "bamdude-diagnostics"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("canonical skill")
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("frontend")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "packages.json").write_text('{"package_version":"test"}')
    (assets / "bamdude-claude-desktop.mcpb").write_bytes(b"build product")
    monkeypatch.setattr(build, "REPO_ROOT", repo)
    assert build.STAGING.resolve().is_relative_to(tmp_path.resolve())
    build.stage_backend(frontend, assets)
    staged = build.STAGING / "app" / "integrations"
    assert (staged / "agent-skills" / "bamdude-diagnostics" / "SKILL.md").read_text() == "canonical skill"
    assert (staged / "ai-plugins" / "dist" / "bamdude-claude-desktop.mcpb").read_bytes() == b"build product"


@pytest.mark.parametrize("argv", [[], ["--skip-pip"]])
def test_cad_is_built_exported_and_verified_after_the_backend_also_with_skip_pip(build, monkeypatch, argv):
    calls = []
    staged_python = build.STAGING / "python"

    def note(name, value=None):
        def call(*args, **kwargs):
            calls.append(name if name != "run_helper" else f"run_helper:{args[1][0]}")
            return value

        return call

    for name, value in {
        "stage_embedded_python": staged_python,
        "stage_vcruntime": None,
        "install_requirements": None,
        "verify_requirements": None,
        "build_ai_packages": build.BUILD_DIR / "ai-plugins",
        "stage_backend": None,
        "stage_nssm": None,
        "stage_ffmpeg": None,
        "stage_render_runtime": None,
        "build_cad_bundle": build.CAD_ROOT / "b",
        "export_cad_bundle": build.STAGING / "app" / "b",
        "run_helper": None,
        "drop_bytecode": None,
        "write_version_file": None,
    }.items():
        monkeypatch.setattr(build, name, note(name, value))
    monkeypatch.setattr(build, "prior_cad_tree_gone", lambda *a, **k: True)
    monkeypatch.setattr(build, "step_payload_problems", note("step_payload_problems", []))
    monkeypatch.setattr(build, "_report_sizes", lambda *a, **k: None, raising=False)
    (build.REPO_ROOT / "static").exists() or pytest.skip("named skip: no built static/ in this checkout")
    assert build.main(["--skip-frontend", "--allow-non-windows", "--allow-candidate-cad", *argv]) == 0
    tail = [c for c in calls if c in {"stage_backend", "stage_render_runtime"} or not c.startswith("stage_")]
    order = [c for c in tail if c not in {"install_requirements", "verify_requirements"}]
    assert order == [
        "build_ai_packages",
        "stage_backend",
        "stage_render_runtime",
        "build_cad_bundle",
        "export_cad_bundle",
        "run_helper:verify",
        "drop_bytecode",
        "step_payload_problems",
        "write_version_file",
    ]


@pytest.mark.parametrize(
    ("flag", "env", "expected"),
    [
        (False, {"GITHUB_REF": "refs/tags/v0.7.1"}, False),
        (True, {"GITHUB_REF": "refs/tags/v0.7.1"}, RuntimeError),
        (False, {"GITHUB_REF": "refs/tags/v0.7.1", "STEP_PREVIEW_CANDIDATE": "1"}, RuntimeError),
        (True, {"GITHUB_REF": "refs/heads/feature/step-thumbnails"}, True),
        (False, {"GITHUB_REF": "refs/heads/dev", "STEP_PREVIEW_CANDIDATE": "1"}, True),
        (False, {}, False),
    ],
)
def test_a_tag_ref_never_takes_the_candidate_pin(build, flag, env, expected):
    if expected is RuntimeError:
        with pytest.raises(RuntimeError):
            build.cad_candidate_allowed(flag, env)
    else:
        assert build.cad_candidate_allowed(flag, env) is expected


# --- build root, export, inventory -------------------------------------------------------------------------------


def _required(app: Path) -> None:
    """The helper, the catalog, the notices and the probe STEP, as stage_backend copies them."""
    for rel in build_required_files():
        source = ROOT / rel
        target = app / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copyfile(source, target)


def build_required_files() -> list[str]:
    return [
        "backend/app/step_runtime.py",
        "backend/app/services/step_runtime_child.py",
        "backend/app/step_preview_probe.py",
        "backend/app/services/step_thumbnail.py",
        "backend/app/preview_render.py",
        "backend/app/data/step_runtime_manifest.json",
        "backend/app/data/step_runtime_closure.json",
        "backend/app/data/step_runtime_notices.json",
        "backend/app/data/step_runtime_notices",
        "backend/app/data/step_probe/box_mm.step",
    ]


def _pip_that_installs(cmd, **kwargs):
    (Path(cmd[cmd.index("--target") + 1]) / "OCP").mkdir(parents=True)
    return subprocess.CompletedProcess(cmd, 0, "", "")


def _real_install(build, monkeypatch) -> Path:
    """step_runtime.install itself into the build root — only pip and the costly checks replaced."""
    monkeypatch.setattr(sr, "interpreter_tags", lambda: WIN312)
    monkeypatch.setattr(sr, "verify_tree", lambda site, closure: [])
    monkeypatch.setattr(sr, "smoke", lambda site: [])
    return sr.install(build.CAD_ROOT, allow_candidate=True, runner=_pip_that_installs)


def test_the_export_carries_only_the_located_bundle(build, monkeypatch):
    bundle = _real_install(build, monkeypatch)
    root = sr.bundle_root(build.CAD_ROOT)
    assert (root / ".win-x64-cp312.lock").is_file() and (root / ".staging" / "win-x64-cp312").is_dir()  # lock contract
    app = build.STAGING / "app"
    _required(app)
    exported = build.export_cad_bundle(bundle, app)
    assert exported == sr.bundle_dir(app, "win-x64-cp312", sr.load_catalog().digest)
    assert build.step_payload_problems(app) == []
    assert sorted(p.name for p in sr.bundle_root(app).iterdir()) == ["win-x64-cp312"]  # no lock, no .staging


def _clean_app(build, monkeypatch) -> Path:
    bundle = _real_install(build, monkeypatch)
    app = build.STAGING / "app"
    _required(app)
    build.export_cad_bundle(bundle, app)
    assert build.step_payload_problems(app) == []
    return app


@pytest.mark.parametrize(
    ("damage", "problem"),
    [
        ("second_digest", "extra_digest"),
        ("staging", "staging"),
        ("lock", "lock"),
        ("notices_json", "missing:backend/app/data/step_runtime_notices.json"),
        ("probe", "missing:backend/app/data/step_probe/box_mm.step"),
        ("receipt", "receipt_missing"),
    ],
)
def test_the_payload_inventory_refuses(build, monkeypatch, damage, problem):
    app = _clean_app(build, monkeypatch)
    root = sr.bundle_root(app)
    bundle = next((root / "win-x64-cp312").iterdir())
    if damage == "second_digest":
        (root / "win-x64-cp312" / ("e" * 32) / "site").mkdir(parents=True)
    elif damage == "staging":
        (root / ".staging" / "win-x64-cp312").mkdir(parents=True)
    elif damage == "lock":
        (root / ".win-x64-cp312.lock").write_bytes(b"")
    elif damage == "notices_json":
        (app / "backend/app/data/step_runtime_notices.json").unlink()
    elif damage == "probe":
        (app / "backend/app/data/step_probe/box_mm.step").unlink()
    elif damage == "receipt":
        (bundle / "receipt.json").unlink()
    assert any(p.startswith(problem) for p in build.step_payload_problems(app)), build.step_payload_problems(app)


# --- the helper's whole tree, owned by the build -----------------------------------------------------------------


def test_the_helper_runs_in_the_named_interpreter(build, tmp_path):
    script = tmp_path / "who.py"
    script.write_text("import sys\nprint(sys.executable)\n", encoding="utf-8")
    result = build.run_helper(Path(BASE), [], timeout=60, cwd=ROOT, helper=(str(script),))
    assert os.path.samefile(result.stdout.strip(), BASE)
    assert not (build.CAD_OWNER / sr.OWNER_RECORD).exists()  # a proven end takes the owner's record too


def test_a_timeout_in_a_helper_child_leaves_nothing_alive(build, tmp_path):
    marker = tmp_path / "grandchild.pid"
    with pytest.raises(RuntimeError):
        build.run_helper(Path(BASE), [str(marker)], timeout=3, cwd=ROOT, helper=(str(SLEEPY),))
    grandchild = int(marker.read_text())
    _until(lambda: not _alive(grandchild), 15)


def test_the_owner_is_recorded_before_any_launch(build, monkeypatch, tmp_path):
    owner = build.CAD_OWNER / sr.OWNER_RECORD
    seen = []

    def checking(cmd, **kwargs):
        data = json.loads(owner.read_text(encoding="ascii"))
        seen.append(data["pid"])
        return subprocess.CompletedProcess(cmd, 0, "{}", "")

    monkeypatch.setattr(step_runtime_child, "run_tree", checking)
    build.run_helper(Path(BASE), ["select"], timeout=60, cwd=ROOT)
    assert seen == [os.getpid()] and not owner.exists()

    def unproven(cmd, **kwargs):
        raise step_runtime_child.TreeUnproven("not proven")

    monkeypatch.setattr(step_runtime_child, "run_tree", unproven)
    with pytest.raises(RuntimeError):
        build.run_helper(Path(BASE), ["select"], timeout=60, cwd=ROOT)
    assert owner.exists()  # an unproven end keeps every record
    owner.unlink()

    def no_record(path, pid):
        raise OSError(28, "no space left")

    called = []
    monkeypatch.setattr(tree, "record", no_record)
    monkeypatch.setattr(step_runtime_child, "run_tree", lambda *a, **k: called.append(1))
    with pytest.raises(RuntimeError):
        build.run_helper(Path(BASE), ["select"], timeout=60, cwd=ROOT)
    assert called == []  # no owner record, no launch


# --- the next build's preflight ----------------------------------------------------------------------------------

PREVIOUS = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from backend.app import step_runtime as sr
from backend.app.services import part_render_tree as tree
owner = Path(sys.argv[2])
owner.mkdir(parents=True, exist_ok=True)
tree.record(owner / sr.OWNER_RECORD, __import__("os").getpid())
token = tree.launch(owner, sr.INSTALLER)
Path(sys.argv[3]).write_text(token, encoding="ascii")  # at the barrier: launched, nothing spawned yet
time.sleep(120)
"""


def _previous_build(build, tmp_path):
    script = tmp_path / "previous.py"
    script.write_text(PREVIOUS, encoding="utf-8")
    ready = tmp_path / "token"
    proc = subprocess.Popen([sys.executable, str(script), str(ROOT), str(build.CAD_OWNER), str(ready)])
    _until(ready.exists)
    return proc, ready.read_text(encoding="ascii")


def _records(build) -> list[str]:
    return sorted(p.name for p in build.CAD_OWNER.iterdir()) if build.CAD_OWNER.exists() else []


def test_a_live_owner_paused_between_launch_and_spawn_blocks_the_next_build(build, tmp_path, monkeypatch):
    proc, _token = _previous_build(build, tmp_path)
    try:
        before = _records(build)
        assert before == sorted([sr.OWNER_RECORD, f"{sr.INSTALLER}.launch"])
        assert build.prior_cad_tree_gone() is False
        staged = []
        monkeypatch.setattr(build, "stage_embedded_python", lambda *a, **k: staged.append(1))
        assert build.main(["--skip-frontend", "--allow-non-windows"]) == 1
        assert staged == [] and _records(build) == before  # refused before any rmtree; records untouched
    finally:
        proc.kill()
        proc.wait(timeout=30)


def test_an_owner_that_died_in_that_window_is_recovered_only_after_proof(build, tmp_path):
    proc, token = _previous_build(build, tmp_path)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", tree.launch_arg(token)])
    try:
        proc.kill()
        proc.wait(timeout=30)
        assert build.prior_cad_tree_gone() is True  # owner proven dead, then the token's holder ended
        _until(lambda: not _alive(holder.pid), 15)
        assert _records(build) == []
    finally:
        for p in (proc, holder):
            if p.poll() is None:
                p.kill()


def test_a_live_owner_that_has_not_launched_yet_blocks_the_next_build(build):
    """The owner records itself BEFORE the launch: in that window there is no launch at all, and its record alone
    must keep the next build out (and keep itself)."""
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        build.CAD_OWNER.mkdir(parents=True)
        tree.record(build.CAD_OWNER / sr.OWNER_RECORD, sleeper.pid)
        assert build.prior_cad_tree_gone() is False
        assert _records(build) == [sr.OWNER_RECORD]
        sleeper.kill()
        sleeper.wait(timeout=30)
        assert build.prior_cad_tree_gone() is True and _records(build) == []
    finally:
        if sleeper.poll() is None:
            sleeper.kill()


@pytest.mark.parametrize("owner_state", ["missing", "corrupt"])
def test_a_launch_without_a_readable_owner_is_no_proof(build, owner_state):
    build.CAD_OWNER.mkdir(parents=True)
    tree.launch(build.CAD_OWNER, sr.INSTALLER)
    if owner_state == "corrupt":
        (build.CAD_OWNER / sr.OWNER_RECORD).write_text("{not json", encoding="ascii")
    before = _records(build)
    assert build.prior_cad_tree_gone() is False
    assert _records(build) == before


def test_a_previous_builds_unproven_tree_blocks_the_next_build(build, tmp_path):
    build.CAD_OWNER.mkdir(parents=True)
    dead = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    tree.record(build.CAD_OWNER / sr.OWNER_RECORD, dead.pid)  # a record needs the process alive...
    dead.kill()
    dead.wait(timeout=30)  # ...then the owner is gone
    token = tree.launch(build.CAD_OWNER, sr.INSTALLER)
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", tree.launch_arg(token)])
    try:
        tree.record(build.CAD_OWNER / f"{sr.INSTALLER}.pid", live.pid)  # ...but its released bootstrap lives
        assert build.prior_cad_tree_gone() is False
        live.kill()
        live.wait(timeout=30)
        assert build.prior_cad_tree_gone() is True and _records(build) == []
    finally:
        if live.poll() is None:
            live.kill()


# --- the install directory limit ---------------------------------------------------------------------------------


def test_the_app_dir_limit_comes_from_the_catalog(build, monkeypatch):
    # A snapshot of the current pin, not a constant: its deepest DLL at the loader's 250 (installer acceptance S1
    # refused 109 — the library at 259 did not load).
    assert build.step_max_app_dir_len() == 100
    catalog = sr.load_catalog(decode_tags=False)
    wheel = next(a for a in catalog.manifest["artifacts"] if a["key"] == "win-x64-cp312")
    closure = {"artifacts": {wheel["filename"]: [{"path": "x" * 100}]}}
    longer = SimpleNamespace(manifest=catalog.manifest, closure=closure)
    assert build.step_max_app_dir_len(longer) == 100 - (100 - 72)
    staged = build.STAGING
    staged.mkdir(parents=True)
    monkeypatch.setattr(build, "_resolve_installer_version", lambda: "0.0.0")
    build.write_version_file()
    assert "#define StepMaxAppDirLen 100" in (staged / "version.iss").read_text(encoding="utf-8")
    if sr.PATH_LIMIT is not None:  # where step_runtime applies the limit (Windows), it is the build's own
        assert build.WINDOWS_LIBRARY_PATH_LIMIT == sr.PATH_LIMIT


def test_the_limit_covers_the_whole_payload(build, monkeypatch):
    """Whatever lies deepest under {app} bounds the directory, not only the CAD bundle's deepest DLL: a dependency
    with a deeper path than the bundle's would otherwise be laid down past MAX_PATH from a directory Setup accepted."""
    deep = build.STAGING / "python" / "Lib" / "site-packages" / ("d" * 130) / "x.py"
    deep.parent.mkdir(parents=True)
    deep.write_text("", encoding="utf-8")
    rel = len(str(deep.relative_to(build.STAGING)).encode("utf-16-le")) // 2
    assert build.payload_max_app_dir_len(build.STAGING) == build.WINDOWS_PATH_LIMIT - 1 - rel
    monkeypatch.setattr(build, "_resolve_installer_version", lambda: "0.0.0")
    build.write_version_file()
    limit = min(build.step_max_app_dir_len(), build.WINDOWS_PATH_LIMIT - 1 - rel)
    assert limit < 100 and f"#define StepMaxAppDirLen {limit}" in (build.STAGING / "version.iss").read_text("utf-8")


def _units(build, path):
    return len(str(path.relative_to(build.STAGING)).encode("utf-16-le")) // 2


@pytest.mark.parametrize("suffix", [".dll", ".pyd"])
def test_a_native_library_in_the_payload_is_bounded_by_the_loaders_limit(build, suffix):
    """Python's own extension modules, PostgreSQL's and Node's libraries load by the same loader as the CAD bundle's:
    a native library anywhere under {app} is held to 250, any other file to MAX_PATH - 1 (installer acceptance S1)."""
    native = build.STAGING / "python" / "Lib" / "site-packages" / ("n" * 100) / f"m{suffix}"
    other = build.STAGING / "app" / "static" / ("t" * 114) / "page.js"
    for path in (native, other):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"")
    assert _units(build, other) > _units(build, native)  # the deeper file is not the one that binds
    bound = build.WINDOWS_LIBRARY_PATH_LIMIT - 1 - _units(build, native)
    assert build.payload_max_app_dir_len(build.STAGING) == bound
    assert bound < build.WINDOWS_PATH_LIMIT - 1 - _units(build, other)


def test_an_executable_is_bounded_like_any_file(build):
    """The loader's limit was measured for libraries; an .exe is held to MAX_PATH - 1 like any other file (owner,
    2026-10-18) — the libraries it loads beside it are bounded by their own suffix."""
    exe = build.STAGING / "app" / "runtime" / ("e" * 120) / "tool.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert build.payload_max_app_dir_len(build.STAGING) == build.WINDOWS_PATH_LIMIT - 1 - _units(build, exe)


def test_the_payload_ships_no_bytecode_the_verify_wrote(build):
    """The final verify imports the staged backend in the helper AND in the probe it starts with an allowlist
    environment (no PYTHONDONTWRITEBYTECODE reaches it: a real build left three caches). stage_backend leaves
    caches out on purpose, so they go again after the verify, the sources stay (final review M8)."""
    package = build.STAGING / "app" / "backend" / "app"
    (package / "__pycache__").mkdir(parents=True)
    (package / "__pycache__" / "preview_render.cpython-312.pyc").write_bytes(b"\x00")
    (package / "services" / "__pycache__").mkdir(parents=True)
    (package / "preview_render.py").write_text("", encoding="utf-8")
    build.drop_bytecode(build.STAGING / "app" / "backend")
    assert not list((build.STAGING / "app" / "backend").rglob("__pycache__"))
    assert (package / "preview_render.py").is_file()
