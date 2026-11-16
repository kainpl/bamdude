"""The PDF first-page thumbnail: one operation of the disposable preview child, and only there.

The child cases run the REAL ``backend.app.preview_render`` process over PDFs written by hand — no fake renderer."""

import ast
import io
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.services import preview_protocol as protocol
from backend.app.services.preview_protocol import Command, PreviewError, attempt_kind
from backend.tests import pdf_fixtures

ROOT = Path(__file__).resolve().parents[4]
APP = ROOT / "backend" / "app"


def _deadline(seconds: float = 120) -> int:
    return time.monotonic_ns() + int(seconds * 1e9)


def _child(root: Path, content: bytes) -> tuple[int, dict]:
    (root / "document.pdf").write_bytes(content)
    request = {"root": str(root), "operation": "pdf_png", "kind": "pdf", "deadline": _deadline()}
    done = subprocess.run(
        [sys.executable, "-m", "backend.app.preview_render"],
        input=json.dumps(request) + "\n",
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return done.returncode, json.loads((root / "result.json").read_text(encoding="utf-8"))


# --- the real child --------------------------------------------------------------------------------------------------


def test_the_real_child_renders_the_first_page_to_a_bounded_png(tmp_path):
    from PIL import Image

    code, result = _child(tmp_path, pdf_fixtures.page_pdf(200, 300))
    assert (code, result["outcome"]) == (0, "ok")
    with Image.open(tmp_path / "preview.png") as picture:
        assert picture.format == "PNG"
        assert max(picture.size) == protocol.PDF_THUMBNAIL_PIXELS and picture.size == (342, 512)
        rgb = picture.convert("RGB")
        # the page's own drawing, not a blank sheet: black over the lower-left quarter, white paper elsewhere
        assert rgb.getpixel((5, 500)) == (0, 0, 0) and rgb.getpixel((330, 5)) == (255, 255, 255)
    assert (tmp_path / "preview.png").stat().st_size <= protocol.PDF_PNG_BYTES


@pytest.mark.parametrize(
    ("content", "outcome", "why"),
    [
        (pdf_fixtures.BROKEN_PDF, "render_failed", "pdf_unreadable"),
        (pdf_fixtures.empty_pdf(), "render_failed", "pdf_unreadable"),
        (pdf_fixtures.encrypted_pdf(), "render_failed", "pdf_encrypted"),
        (b"", "render_failed", "pdf_unreadable"),
        (pdf_fixtures.page_pdf(20000, 20000), "resource_limit", "pdf_page_size"),
        (pdf_fixtures.page_pdf(200, 14401), "resource_limit", "pdf_page_size"),
    ],
    ids=["broken", "no-pages", "encrypted", "zero-bytes", "oversized-page", "one-side-over"],
)
def test_the_real_child_ends_a_pdf_it_cannot_picture_with_a_closed_reason(tmp_path, content, outcome, why):
    code, result = _child(tmp_path, content)
    assert code == 0 and not (tmp_path / "preview.png").exists()
    assert (result["outcome"], result["why"]) == (outcome, why)
    assert str(tmp_path) not in json.dumps(result)


def test_a_page_at_the_size_limit_is_still_pictured(tmp_path):
    from PIL import Image

    code, result = _child(tmp_path, pdf_fixtures.page_pdf(protocol.PDF_PAGE_POINTS, 10))
    assert (code, result["outcome"]) == (0, "ok")
    with Image.open(tmp_path / "preview.png") as picture:
        assert picture.size[0] == protocol.PDF_THUMBNAIL_PIXELS and picture.size[1] >= 1


def test_a_child_without_the_renderer_says_so(tmp_path, monkeypatch):
    """A broken install (the module cannot be imported) is a clean «no thumbnail» with its own reason."""
    from backend.app import preview_render

    (tmp_path / "document.pdf").write_bytes(pdf_fixtures.page_pdf())
    monkeypatch.setitem(sys.modules, "pypdfium2", None)  # import pypdfium2 -> ImportError
    request = {"root": str(tmp_path), "operation": "pdf_png", "kind": "pdf", "deadline": _deadline()}
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO((json.dumps(request) + "\n").encode())))
    assert preview_render.main() == 0
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert (result["outcome"], result["why"]) == ("render_failed", "pdf_renderer_missing")
    assert not (tmp_path / "preview.png").exists()


# --- main never imports the renderer ---------------------------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_only_the_childs_module_imports_pdfium():
    """Two users, and the second opens no upload: ``label_picture`` rasterises the label PDF that
    ``render_template_pdf`` produced a moment earlier from a design ``label_gate`` already bounded — a document
    main wrote itself, not an untrusted file. Every PDF a person uploaded stays with the child's module."""
    users = sorted(
        path.relative_to(APP).as_posix()
        for path in APP.rglob("*.py")
        if any(name.split(".")[0].startswith("pypdfium2") for name in _imports(path))
    )
    assert users == ["services/label_picture.py", "services/pdf_thumbnail.py"]


def test_only_the_render_child_imports_the_pdf_module():
    users = sorted(
        path.relative_to(APP).as_posix()
        for path in APP.rglob("*.py")
        if any(name.endswith("pdf_thumbnail") for name in _imports(path))
    )
    assert users == ["preview_render.py"]


def test_importing_the_app_side_of_the_chain_loads_no_pdfium():
    """A fresh interpreter: main's owners of the chain import, and PDFium is not loaded."""
    code = (
        "import sys\n"
        "import backend.app.preview_render, backend.app.preview_service\n"
        "import backend.app.services.preview_runtime, backend.app.services.library_preview\n"
        "print(sorted(m for m in sys.modules if m.startswith('pypdfium2')))\n"
    )
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip() == "[]"


# --- the wire ----------------------------------------------------------------------------------------------------------


def _command(*refs):
    value = {
        "protocol_version": protocol.VERSION,
        "app_generation": "a",
        "service_epoch": "b",
        "attempt_id": uuid.uuid4().hex,
        "attempt_seq": 1,
        "operation": "run",
        "deadline_monotonic_ns": 123,
        "manifest": [],
        "cad": None,
    }
    value["manifest"] = [
        {"role": role, "key": f"{value['attempt_id']}_{role}", "kind": kind, "size": 10, "digest": "a" * 64}
        for role, kind in refs
    ]
    return value


def test_a_pdf_rides_as_a_document_of_its_own_kind():
    command = Command.parse(_command(("document", "pdf")), "a", "b")
    assert command.kind == "pdf" == attempt_kind(command.manifest)


@pytest.mark.parametrize(
    "refs",
    [
        [("document", "stl")],
        [("document", "png")],
        [("mesh", "pdf")],
        [("sliced", "pdf")],
        [("document", "pdf"), ("mesh", "stl")],
        [("document", "pdf"), ("sliced", "3mf")],
        [("document", "pdf"), ("source_png", "png")],
    ],
)
def test_a_document_is_a_pdf_and_rides_alone(refs):
    with pytest.raises(PreviewError):
        Command.parse(_command(*refs), "a", "b")


def test_the_pdf_child_is_a_known_decider_with_its_own_budgets():
    assert "pdf_png" in protocol.DECIDERS
    assert protocol.PDF_SECONDS < protocol.ACTIVE_SECONDS
    assert protocol.PDF_INPUT_BYTES < protocol.OBJECT_BYTES
    assert protocol.PDF_PNG_BYTES < protocol.PNG_BYTES
    assert protocol.PDF_THUMBNAIL_PIXELS**2 < protocol.PNG_PIXELS
    assert protocol.PDF_RSS_BYTES < protocol.RSS_BYTES
