"""The printer-config sync script automates the byte-for-byte re-copy of
backend/app/data/printers/ from a BambuStudio checkout (mirrors only the
files we already ship; updates the README source tag)."""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "sync_printer_configs.py"


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    src = tmp_path / "checkout" / "resources" / "printers"
    dst = tmp_path / "data" / "printers"
    src.mkdir(parents=True)
    dst.mkdir(parents=True)
    (src / "N6.json").write_text('{"a": 1}', encoding="utf-8")
    (dst / "N6.json").write_text('{"a": 0}', encoding="utf-8")  # stale
    (dst / "README.md").write_text("Source tag: vOLD\n", encoding="utf-8")
    return tmp_path / "checkout", dst


def test_copies_changed_files_and_updates_readme_tag(tmp_path):
    checkout, dst = _setup(tmp_path)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(checkout), "--dest", str(dst), "--tag", "vNEW"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads((dst / "N6.json").read_text(encoding="utf-8")) == {"a": 1}
    assert "N6.json" in result.stdout  # changed file named
    assert "Source tag: vNEW" in (dst / "README.md").read_text(encoding="utf-8")


def test_only_mirrors_existing_dest_files(tmp_path):
    checkout, dst = _setup(tmp_path)
    (checkout / "resources" / "printers" / "ZZ.json").write_text("{}", encoding="utf-8")
    subprocess.run(
        [sys.executable, str(SCRIPT), str(checkout), "--dest", str(dst), "--tag", "v"],
        capture_output=True,
        text=True,
    )
    assert not (dst / "ZZ.json").exists()  # we mirror our set, not BS's whole zoo
