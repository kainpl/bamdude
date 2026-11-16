"""The PDF renderer is an ordinary requirement of every install, and its licence note sits beside its pin."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
REQUIREMENTS = (ROOT / "requirements.txt").read_text(encoding="utf-8")


def _pin() -> str:
    return next(line.strip() for line in REQUIREMENTS.splitlines() if line.strip().lower().startswith("pypdfium2"))


def test_it_is_required_on_every_platform():
    """One unconditional line: no environment marker, no per-platform exclusion."""
    assert _pin() == "pypdfium2>=5.14.0,<6"
    assert len(re.findall(r"(?mi)^\s*pypdfium2\b", REQUIREMENTS)) == 1


def test_every_install_path_installs_the_requirements_file():
    for script in ("Dockerfile", "install/install.sh", "install/update.sh", "installers/windows/build.py"):
        assert "requirements.txt" in (ROOT / script).read_text(encoding="utf-8"), script


def test_the_windows_installer_checks_for_it():
    assert '"pypdfium2"' in (ROOT / "installers" / "windows" / "build.py").read_text(encoding="utf-8")


def test_the_pin_carries_its_licence_note():
    """No notices system exists for ordinary pip packages (their wheels carry their texts); what a maintainer
    must know before building a musl-based image is said where the dependency is declared."""
    start = REQUIREMENTS.index("# First-page thumbnails of library PDFs")
    note = REQUIREMENTS[start : REQUIREMENTS.index("pypdfium2>=", start)]
    for needle in (
        "Apache-2.0 / BSD-3-Clause",
        "PDFium BSD-3-Clause",
        "musllinux",
        "libgcc_s and libstdc++",
        "GPL-3.0-or-later WITH GCC-exception-3.1",
        "does not redistribute",
        "sources ship with every release",
    ):
        assert needle in note, needle
