"""One operation in a disposable process. Staging paths are local bootstrap only."""

import json
import logging
import os
import sys
from pathlib import Path


class _Unpictured(Exception):
    """No picture, for a closed reason the parent log may carry."""

    def __init__(self, why: str):
        super().__init__(why)
        self.why = why


class _Swallowed(logging.Handler):
    """The class of an exception the renderer logged and swallowed (generate_stl_thumbnail then answers None): the
    class only — the logged text carries the source path."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.why = None

    def emit(self, record):
        args = record.args if isinstance(record.args, tuple) else ()
        exc = record.exc_info[1] if record.exc_info else next((a for a in args if isinstance(a, BaseException)), None)
        if exc is not None:
            self.why = type(exc).__name__[:64]


def _checked(check, why: str) -> None:
    """A validator's refusal, named: render_failed as ``why``, a count over its cap as triangle_cap."""
    from backend.app.services.preview_protocol import PreviewError

    try:
        check()
    except PreviewError as exc:
        if exc.outcome == "render_failed":
            raise PreviewError("render_failed", why=why) from None
        if exc.outcome == "resource_limit":
            raise PreviewError("resource_limit", why="triangle_cap") from None
        raise


def _picture(generated, root: Path, deadline, swallowed: _Swallowed) -> None:
    from backend.app.services.preview_artifacts import validate

    if not generated:
        raise _Unpictured(swallowed.why or "no_picture")
    os.replace(generated, root / "preview.png")
    _checked(lambda: validate(root / "preview.png", "png", deadline), "png_invalid")


def main():
    from backend.app.services.preview_artifacts import validate
    from backend.app.services.preview_protocol import PreviewError, remaining

    request = json.loads(sys.stdin.buffer.readline(16385))
    root = Path(request["root"])
    deadline = request["deadline"]
    remaining(deadline)
    operation = request["operation"]
    result = {"pid": os.getpid(), "outcome": "render_failed"}
    swallowed = _Swallowed()
    logging.getLogger("backend.app.services.stl_thumbnail").addHandler(swallowed)
    try:
        if operation == "mesh":
            from backend.app.services.stl_thumbnail import generate_stl_thumbnail

            source = root / ("mesh." + request["kind"])
            _picture(generate_stl_thumbnail(source, root), root, deadline, swallowed)
            result["outcome"] = "ok"
        elif operation == "cad_png":
            # The PNG phase of a STEP attempt: an allocation backstop before the renderer is imported, then the
            # converter's STL checked again (count, size, finiteness) before trimesh reads it.
            from backend.app.services.preview_artifacts import validate_binary_stl
            from backend.app.services.preview_protocol import CAD_TRIANGLES, STEP_DATA_LIMIT_BYTES

            if sys.platform.startswith("linux"):
                import resource

                _soft, hard = resource.getrlimit(resource.RLIMIT_DATA)
                cap = STEP_DATA_LIMIT_BYTES if hard == resource.RLIM_INFINITY else min(STEP_DATA_LIMIT_BYTES, hard)
                resource.setrlimit(resource.RLIMIT_DATA, (cap, cap))
            _checked(lambda: validate_binary_stl(root / "mesh.stl", CAD_TRIANGLES, deadline), "mesh_invalid")
            from backend.app.services.stl_thumbnail import generate_stl_thumbnail

            _picture(generate_stl_thumbnail(root / "mesh.stl", root), root, deadline, swallowed)
            result["outcome"] = "ok"
        elif operation == "pdf_png":
            # A library PDF's first page. PDFium is loaded here and nowhere else: this process is the renderer.
            from backend.app.services.pdf_thumbnail import render_first_page

            render_first_page(root / "document.pdf", root / "preview.png", deadline)
            _checked(lambda: validate(root / "preview.png", "png", deadline), "png_invalid")
            result["outcome"] = "ok"
        elif operation == "source":
            from backend.app.services.preview_transform import inject_source

            inject_source(root, deadline)
            result["outcome"] = "ok"
        elif operation == "plates":
            from backend.app.services.plate_thumbnail import inject_plate_thumbnails_if_missing

            source = root / "sliced.3mf"
            validate(source, "3mf", deadline)
            (root / "output.3mf").write_bytes(inject_plate_thumbnails_if_missing(source.read_bytes()))
            validate(root / "output.3mf", "3mf", deadline)
            result["outcome"] = "ok"
        else:
            raise PreviewError("protocol_error")
    except _Unpictured as exc:
        result["why"] = exc.why
    except PreviewError as exc:
        result["outcome"] = exc.outcome
        if exc.why:
            result["why"] = exc.why
    except Exception as exc:
        # The parent emits one bounded outcome: the class names the failure, never its text (user paths/secrets).
        result["why"] = type(exc).__name__[:64]
    finally:
        logging.getLogger("backend.app.services.stl_thumbnail").removeHandler(swallowed)
    (root / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
