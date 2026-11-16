"""The PDF renderer is an ordinary requirement of every install, and its licence note sits beside its pin."""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
REQUIREMENTS = (ROOT / "requirements.txt").read_text(encoding="utf-8")


def _pin() -> str:
    return next(line.strip() for line in REQUIREMENTS.splitlines() if line.strip().lower().startswith("pypdfium2"))


def test_it_is_required_on_every_platform():
    """One unconditional line: no environment marker, no per-platform exclusion."""
    assert _pin() == "pypdfium2>=5.14.0,<6"
    assert len(re.findall(r"(?mi)^\s*pypdfium2\b", REQUIREMENTS)) == 1


_PIP_INSTALLS_THE_FILE = re.compile(r"""\bpip["']?\s+install\b[^#\n]*?\s-r\s+["']?[^\s"']*requirements\.txt""")


def _commands(text: str) -> list[str]:
    """The lines of a shell script or Dockerfile that RUN something: no comments, nothing merely printed."""
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or re.match(r"(echo|printf|warn|info|log)\b", line):
            continue
        lines.append(line)
    return lines


@pytest.mark.parametrize("script", ["Dockerfile", "install/install.sh", "install/update.sh"])
def test_every_shell_install_path_runs_pip_install_on_the_requirements_file(script):
    """A command, not a mention: the line is neither a comment nor an ``echo`` of what to type."""
    commands = _commands((ROOT / script).read_text(encoding="utf-8"))
    assert any(_PIP_INSTALLS_THE_FILE.search(line) for line in commands), script


def test_the_reading_of_a_script_ignores_what_does_not_run():
    script = """
    # pip install -r requirements.txt
    echo "pip install -r requirements.txt"
    COPY requirements.txt ./
    """
    assert not any(_PIP_INSTALLS_THE_FILE.search(line) for line in _commands(script))
    assert _PIP_INSTALLS_THE_FILE.search('"$INSTALL_PATH/venv/bin/pip" install -r requirements.txt')
    assert _PIP_INSTALLS_THE_FILE.search("&& pip install --root-user-action=ignore -r requirements.txt \\")
    assert _PIP_INSTALLS_THE_FILE.search("${INSTALL_DIR}/venv/bin/pip install -r ${INSTALL_DIR}/requirements.txt")


def test_the_windows_build_runs_pip_install_on_the_requirements_file():
    """``installers/windows/build.py`` builds its command as a list: read it off the syntax tree."""
    tree = ast.parse((ROOT / "installers" / "windows" / "build.py").read_text(encoding="utf-8"))
    installers = []
    for function in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)):
        # A name bound to ``<something> / "requirements.txt"`` in this function…
        files = {
            target.id
            for node in ast.walk(function)
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.BinOp)
            for target in node.targets
            if isinstance(target, ast.Name)
            and isinstance(node.value.right, ast.Constant)
            and node.value.right.value == "requirements.txt"
        }
        for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
            if ast.unparse(call.func) != "subprocess.run" or not call.args or not isinstance(call.args[0], ast.List):
                continue
            words = [ast.unparse(e) if not isinstance(e, ast.Constant) else e.value for e in call.args[0].elts]
            # …handed to ``-m pip install … -r <that file>``.
            if "-r" not in words or words.index("-r") + 1 >= len(words):
                continue
            after = words[words.index("-r") + 1]
            if "pip" in words and "install" in words and any(after == f"str({name})" for name in files):
                installers.append(function.name)
    assert installers == ["install_requirements"]
    called = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert "install_requirements" in called, "the function that installs the requirements is never called"


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


def test_the_note_says_who_imports_it():
    """Two importers, both named: the preview child for uploads, and the one label module of main that
    rasterises PDFs BamDude generated itself (review B-M9 — the note used to say "never by the main process")."""
    start = REQUIREMENTS.index("# First-page thumbnails of library PDFs")
    note = " ".join(
        line.lstrip("# ").strip()
        for line in REQUIREMENTS[start : REQUIREMENTS.index("pypdfium2>=", start)].splitlines()
    )
    assert "services/pdf_thumbnail.py" in note and "services/label_picture.py" in note
    assert "never by the main process" not in note
