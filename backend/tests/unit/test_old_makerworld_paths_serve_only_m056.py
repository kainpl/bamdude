"""``services/makerworld.py`` and ``services/makerworld_meta.py`` are shims for m056 alone.

MakerWorld lives in ``services/model_providers/makerworld/`` (upstream #2845).
The two old modules stay only because the released migration m056 imports
them, and migrations are frozen. New code importing the old paths would keep
a second front door open to the provider — so only m056 may.
"""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
OLD_PATHS = {"backend.app.services.makerworld", "backend.app.services.makerworld_meta"}
ALLOWED = {Path("migrations") / "m056_library_file_makerworld_meta.py"}


def test_only_m056_imports_the_old_makerworld_modules():
    offenders = []
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP)
        if rel in ALLOWED:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module in OLD_PATHS:
                offenders.append(f"{rel}:{node.lineno} {node.module}")
            elif isinstance(node, ast.Import):
                offenders.extend(f"{rel}:{node.lineno} {a.name}" for a in node.names if a.name in OLD_PATHS)
    assert not offenders, offenders


def test_the_shims_re_export_what_m056_uses():
    from backend.app.services import makerworld, makerworld_meta
    from backend.app.services.model_providers.makerworld import errors, meta, service

    assert makerworld.MakerWorldService is service.MakerWorldService
    assert makerworld.MakerWorldError is errors.MakerWorldError
    assert makerworld_meta.build_meta_dict is meta.build_meta_dict
    assert makerworld_meta.download_covers is meta.download_covers
