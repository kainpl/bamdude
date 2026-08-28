"""The distiller turns a slicer checkout's BBL filament profiles into the
compact identity catalog (backend/app/data/filament_catalog/). Chain-merge,
template exclusion, loud unresolvables, deterministic output — plus a golden
test over the SHIPPED files."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "distill_filament_catalog.py"


def _write(profile_dir: Path, name: str, payload: dict) -> None:
    (profile_dir / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def fake_checkout(tmp_path: Path) -> Path:
    fil = tmp_path / "resources" / "profiles" / "BBL" / "filament"
    fil.mkdir(parents=True)
    _write(
        fil,
        "fdm_filament_common",
        {
            "name": "fdm_filament_common",
            "instantiation": "false",
            "filament_type": ["PLA"],
            "filament_vendor": ["Generic"],
            "nozzle_temperature_range_low": ["190"],
            "nozzle_temperature_range_high": ["240"],
            "filament_is_support": ["0"],
        },
    )
    _write(
        fil,
        "fdm_filament_pet",
        {
            "name": "fdm_filament_pet",
            "instantiation": "false",
            "inherits": "fdm_filament_common",
            "filament_type": ["PETG"],
            "nozzle_temperature_range_low": ["220"],
            "nozzle_temperature_range_high": ["260"],
        },
    )
    _write(
        fil,
        "Test PETG @base",
        {
            "name": "Test PETG @base",
            "instantiation": "false",
            "inherits": "fdm_filament_pet",
            "filament_id": "GFT99",
            "nozzle_temperature_range_high": ["270"],
        },
    )
    _write(
        fil,
        "Test PETG @BBL A1M",
        {
            "name": "Test PETG @BBL A1M",
            "instantiation": "true",
            "inherits": "Test PETG @base",
            "setting_id": "GFST99_00",
            "compatible_printers": ["Bambu Lab A1 mini 0.4 nozzle"],
        },
    )
    # A leaf whose chain never yields a filament_id — must be reported, not silently kept.
    _write(
        fil,
        "Broken @BBL A1M",
        {
            "name": "Broken @BBL A1M",
            "instantiation": "true",
            "inherits": "fdm_filament_pet",
            "setting_id": "GFSBRK_00",
            "compatible_printers": ["Bambu Lab A1 mini 0.4 nozzle"],
        },
    )
    return tmp_path


def _run(checkout: Path, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(checkout), "--out", str(out), "--tag", "vTEST"],
        capture_output=True,
        text=True,
    )


def test_distills_family_and_preset_with_merged_chain(fake_checkout, tmp_path):
    out = tmp_path / "cat.json"
    result = _run(fake_checkout, out)
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["source"]["tag"] == "vTEST"
    fam = next(f for f in data["families"] if f["filament_id"] == "GFT99")
    assert fam["alias"] == "Test PETG"
    assert fam["filament_type"] == "PETG"  # from fdm_filament_pet
    assert fam["vendor"] == "Generic"  # from fdm_filament_common
    preset = next(p for p in data["presets"] if p["setting_id"] == "GFST99_00")
    assert preset["filament_id"] == "GFT99"
    assert preset["nozzle_temp"] == [220, 270]  # low from pet, high overridden by @base
    assert preset["compatible_printers"] == ["Bambu Lab A1 mini 0.4 nozzle"]


def test_templates_are_not_presets(fake_checkout, tmp_path):
    out = tmp_path / "cat.json"
    _run(fake_checkout, out)
    names = [p["name"] for p in json.loads(out.read_text(encoding="utf-8"))["presets"]]
    assert "Test PETG @base" not in names and "fdm_filament_pet" not in names


def test_unresolvable_leaf_is_reported_and_excluded(fake_checkout, tmp_path):
    out = tmp_path / "cat.json"
    result = _run(fake_checkout, out)
    assert "Broken @BBL A1M" in result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert all(p["setting_id"] != "GFSBRK_00" for p in data["presets"])


def test_vendor_index_subdirectories_are_followed(fake_checkout, tmp_path):
    """Orca's BBL.json lists profiles under sub_paths like filament/Polymaker/…
    — the distiller must follow the index, not just glob the top level."""
    profiles_root = fake_checkout / "resources" / "profiles"
    poly = profiles_root / "BBL" / "filament" / "Polymaker"
    poly.mkdir(parents=True)
    _write(
        poly,
        "Poly PLA @base",
        {
            "name": "Poly PLA @base",
            "instantiation": "false",
            "inherits": "fdm_filament_common",
            "filament_id": "GFP42",
        },
    )
    _write(
        profiles_root / "BBL" / "filament",
        "Poly PLA @BBL A1M",
        {
            "name": "Poly PLA @BBL A1M",
            "instantiation": "true",
            "inherits": "Poly PLA @base",
            "setting_id": "GFSP42_00",
            "compatible_printers": ["Bambu Lab A1 mini 0.4 nozzle"],
        },
    )
    index = {
        "name": "BBL",
        "filament_list": [
            {"name": "Poly PLA @base", "sub_path": "filament/Polymaker/Poly PLA @base.json"},
            {"name": "Poly PLA @BBL A1M", "sub_path": "filament/Poly PLA @BBL A1M.json"},
            {"name": "fdm_filament_common", "sub_path": "filament/fdm_filament_common.json"},
        ],
    }
    (profiles_root / "BBL.json").write_text(json.dumps(index), encoding="utf-8")

    out = tmp_path / "cat.json"
    result = _run(fake_checkout, out)
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert any(f["filament_id"] == "GFP42" for f in data["families"])
    preset = next(p for p in data["presets"] if p["setting_id"] == "GFSP42_00")
    assert preset["filament_id"] == "GFP42"
    # Files not named by the index are still swept up by the glob fallback:
    assert any(p["setting_id"] == "GFST99_00" for p in data["presets"])


def test_output_is_sorted_and_deterministic(fake_checkout, tmp_path):
    out1, out2 = tmp_path / "a.json", tmp_path / "b.json"
    _run(fake_checkout, out1)
    _run(fake_checkout, out2)
    assert out1.read_text(encoding="utf-8") == out2.read_text(encoding="utf-8")
    data = json.loads(out1.read_text(encoding="utf-8"))
    assert data["presets"] == sorted(data["presets"], key=lambda p: p["name"])


def test_shipped_bambu_catalog_golden():
    data = json.loads(
        (REPO / "backend" / "app" / "data" / "filament_catalog" / "bambu.json").read_text(encoding="utf-8")
    )
    fam = {f["filament_id"]: f for f in data["families"]}
    assert fam["GFG99"]["alias"] == "Generic PETG"
    assert fam["GFA00"]["alias"] == "Bambu PLA Basic"
    preset = next(p for p in data["presets"] if p["setting_id"] == "GFSG99_00")
    assert preset["filament_id"] == "GFG99"
    assert "Bambu Lab A1 mini 0.4 nozzle" in preset["compatible_printers"]
    # 2026-08-22 cross-validation: the live Bambu cloud public listing carries
    # exactly 1928 filament rows — the distilled count matches it.
    assert len(data["presets"]) == 1928


def test_shipped_orca_catalog_golden():
    data = json.loads(
        (REPO / "backend" / "app" / "data" / "filament_catalog" / "orca.json").read_text(encoding="utf-8")
    )
    fam = {f["filament_id"]: f for f in data["families"]}
    assert fam["GFG99"]["alias"] == "Generic PETG"
    # The name Orca-cloud children put into `inherits` must exist as a preset:
    assert any(p["name"] == "Generic PETG @BBL A1M" for p in data["presets"])
