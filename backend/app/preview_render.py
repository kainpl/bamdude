"""One operation in a disposable process. Staging paths are local bootstrap only."""

import json
import os
import sys
from pathlib import Path


def main():
    from backend.app.services.preview_artifacts import validate
    from backend.app.services.preview_protocol import PreviewError, remaining

    request = json.loads(sys.stdin.buffer.readline(16385))
    root = Path(request["root"])
    deadline = request["deadline"]
    remaining(deadline)
    operation = request["operation"]
    result = {"pid": os.getpid(), "outcome": "render_failed"}
    try:
        if operation == "mesh":
            from backend.app.services.stl_thumbnail import generate_stl_thumbnail

            source = root / ("mesh." + request["kind"])
            generated = generate_stl_thumbnail(source, root)
            if generated:
                os.replace(generated, root / "preview.png")
                validate(root / "preview.png", "png", deadline)
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
    except PreviewError as exc:
        result["outcome"] = exc.outcome
    except Exception:
        pass  # parent emits one bounded outcome, no user paths/secrets
    (root / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
