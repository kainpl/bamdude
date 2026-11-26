"""Which code may ask for a STEP picture (spec §3, plan A5 negative callsites): the shared upload and the ZIP door
through ``_queue_step_thumbnail``, and the explicit button. MakerWorld's save, the archive, the printer's files,
the Virtual Printer, calibration and the scanner never do — a new caller has to be decided, not drift in."""

from __future__ import annotations

import ast
import functools
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"

ALLOWED_QUEUE_CALLERS = {
    ("api/routes/library.py", "store_library_upload"),
    ("api/routes/library.py", "extract_zip_file"),
}
ALLOWED_ENQUEUE_MODULES = {"api/routes/library.py", "api/routes/library_thumbnail_jobs.py"}

# Every model site is a provider under ``services/model_providers/``. MakerWorld is reached through the routes
# in ``api/routes/makerworld.py``, a source with a catalogue (Manyfold) through ``api/routes/model_sources.py``,
# and both put a file in the library through ONE import core, ``services/model_import.py`` — the door a STEP
# from a model site actually comes in by.
NEGATIVE_DOORS = (
    "services/model_providers",
    "services/model_import.py",
    "api/routes/makerworld.py",
    "api/routes/model_sources.py",
    "services/archive.py",
    "services/virtual_printer",
    "services/calibration_service.py",
    "services/library_scan.py",
)


_SCANNED_CALLS = frozenset({"_queue_step_thumbnail", "enqueue"})


@functools.cache
def _callers() -> dict[str, frozenset[tuple[str, str]]]:
    """``{called name: {(module, enclosing function)}}`` for the names the tests ask about, in ONE pass.

    Every enclosing function of a call counts, a nested call's outer functions included, as the
    per-name walk this replaced counted them; the tree is parsed once per process, not once per name.
    """
    found: dict[str, set[tuple[str, str]]] = {name: set() for name in _SCANNED_CALLS}
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = path.relative_to(APP).as_posix()
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            called = set()
            for node in ast.walk(function):
                if isinstance(node, ast.Call):
                    target = node.func
                    called.add(target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None))
            for name in _SCANNED_CALLS & called:
                found[name].add((module, function.name))
    return {name: frozenset(pairs) for name, pairs in found.items()}


def _calls(name: str) -> frozenset[tuple[str, str]]:
    return _callers()[name]


def test_only_the_shared_upload_and_the_zip_door_ask_for_a_new_files_picture():
    assert set(_calls("_queue_step_thumbnail")) == ALLOWED_QUEUE_CALLERS


def test_only_the_doors_and_the_button_enqueue_a_step_job():
    modules = {module for module, _ in _calls("enqueue")}
    jobs_internal = {"services/library_preview_jobs.py"}
    unexpected = modules - ALLOWED_ENQUEUE_MODULES - jobs_internal - _other_enqueue_owners()
    assert not unexpected, unexpected


def _other_enqueue_owners() -> set[str]:
    """Modules whose own ``enqueue`` is a different queue (not the STEP jobs) — named, never assumed."""
    owners = set()
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if ".enqueue(" in text and "library_preview_jobs" not in text and "_queue_step_thumbnail" not in text:
            owners.add(path.relative_to(APP).as_posix())
    return owners


def test_the_negative_doors_never_name_the_step_jobs():
    for relative in NEGATIVE_DOORS:
        target = APP / relative
        files = [target] if target.is_file() else list(target.rglob("*.py"))
        # a door that moved is a door nobody watches: the list named ``services/makerworld`` for months after
        # the code went to ``services/model_providers/`` and asserted nothing about it
        assert files, f"{relative} is gone - name the place its code moved to"
        for path in files:
            assert not _asks_for_a_step_picture(path.read_text(encoding="utf-8")), path


def _asks_for_a_step_picture(text: str) -> bool:
    return "library_preview_jobs" in text or "_queue_step_thumbnail" in text


def test_the_import_core_asking_for_a_step_picture_would_be_caught():
    """The mutation, kept as a test: the import core as it is passes, and the same source with either way
    of asking for a STEP thumbnail added does not."""
    core = (APP / "services" / "model_import.py").read_text(encoding="utf-8")
    assert "services/model_import.py" in NEGATIVE_DOORS and not _asks_for_a_step_picture(core)
    through_the_door = core + "\n    await library_routes._queue_step_thumbnail(db, library_file, authority)\n"
    through_the_queue = core + "\nfrom backend.app.services import library_preview_jobs\n"
    assert _asks_for_a_step_picture(through_the_door) and _asks_for_a_step_picture(through_the_queue)


def test_only_three_modules_start_the_converter():
    """The converter is the one importer of OCP, so who starts it is decided, not found: the preview service (a
    picture), the assembly service (a print-grade mesh for "Assemble project", stage C of its spec) and the
    runtime's own smoke check. The assembly never asks for a PICTURE — it names no thumbnail job."""
    starters = {
        path.relative_to(APP).as_posix()
        for path in APP.rglob("*.py")
        if "backend.app.services.step_thumbnail" in path.read_text(encoding="utf-8")
    }
    # the converter names itself in its docstring, and the guardian only lists what it may be asked to run
    starters -= {"services/step_thumbnail.py", "worker_guardian.py"}
    assert starters == {"preview_service.py", "assembly_service.py", "step_runtime.py"}
    for relative in ("assembly_service.py", "assembly_child.py", "services/assembly", "api/routes/library_assembly.py"):
        target = APP / relative
        for path in [target] if target.is_file() else target.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "library_preview_jobs" not in text and "_queue_step_thumbnail" not in text, path
