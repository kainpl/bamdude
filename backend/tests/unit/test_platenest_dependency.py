"""The packing engine of "Assemble project" is pinned exactly: we write files with it and read them back."""

import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[3]


def _pin() -> str:
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return next(line.strip() for line in lines if line.strip().lower().startswith("platenest"))


def test_requirements_pin_the_installed_version_exactly():
    requirement = Requirement(_pin())
    assert requirement.name == "platenest"
    assert requirement.extras == {"optimize"}
    assert requirement.url is None
    installed = version("platenest")
    assert str(requirement.specifier) == f"=={installed}"
    from platenest import optimize

    assert callable(optimize)


def test_the_windows_installer_checks_for_it():
    assert '"platenest"' in (ROOT / "installers" / "windows" / "build.py").read_text(encoding="utf-8")


def test_the_3mf_module_pulls_in_neither_numpy_nor_opencv():
    code = "import platenest.bambu3mf, sys; assert 'cv2' not in sys.modules and 'numpy' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)
