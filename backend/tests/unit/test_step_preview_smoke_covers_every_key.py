"""Every bundle key of the STEP-preview catalog has a smoke row that checks it (spec §9, plan A7): the runner it
promises, the Python it names, the requires_cad lane run strictly, and the armv7 job deciding without a download."""

from pathlib import Path

import yaml

from backend.app import step_runtime

ROOT = Path(__file__).resolve().parents[3]
SMOKE = ROOT / ".github" / "workflows" / "step-preview-smoke.yml"


def _jobs() -> dict:
    return yaml.safe_load(SMOKE.read_text(encoding="utf-8"))["jobs"]


def _runs(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_every_catalog_key_has_exactly_one_row():
    keys = [a["key"] for a in step_runtime.load_catalog().manifest["artifacts"] if a["key"]]
    rows = [row["key"] for row in _jobs()["smoke"]["strategy"]["matrix"]["include"]]
    assert sorted(rows) == sorted(keys)


def test_a_row_runs_the_python_its_key_names():
    for row in _jobs()["smoke"]["strategy"]["matrix"]["include"]:
        major, minor = row["python"].split(".")
        assert row["key"].endswith(f"-cp{major}{minor}"), row


def test_every_row_checks_the_key_it_promises_and_runs_the_lane_strictly():
    job = _jobs()["smoke"]
    runs = _runs(job)
    assert "step_runtime select" in runs and "matrix.key" in runs
    assert "-m requires_cad" in runs and job["env"]["BAMDUDE_REQUIRE_CAD"] == "1"
    assert "step_runtime verify" in runs and "step_render_probe" in runs


def test_the_armv7_job_decides_without_a_network_and_renders_an_stl():
    job = _jobs()["armv7"]
    runs = _runs(job)
    assert "--network none" in runs and "linux/arm/v7" in runs
    assert (
        "step_preview_armv7_smoke.sh" in runs and "step_preview_armv7_stl_smoke.sh" in runs
    )  # app/STL, not only exit 3
    for script in ("step_preview_armv7_smoke.sh", "step_preview_armv7_stl_smoke.sh"):
        assert (ROOT / "scripts" / "ci" / script).is_file()


def test_the_scheduled_pin_check_can_write_nothing():
    check = yaml.safe_load((ROOT / ".github" / "workflows" / "step-preview-pin-check.yml").read_text(encoding="utf-8"))
    assert check["permissions"] == {"contents": "read"}
    assert all("permissions" not in job for job in check["jobs"].values())
    runs = "\n".join(step.get("run", "") for job in check["jobs"].values() for step in job["steps"])
    assert "pin_step_runtime.py --upstream" in runs
    for verb in ("git push", "gh pr", "gh issue", "git commit"):
        assert verb not in runs
