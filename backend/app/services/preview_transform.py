"""Pure source-preview transform, imported only by the render child."""

import shutil
import zipfile
from pathlib import Path

from backend.app.services.preview_artifacts import validate
from backend.app.services.preview_protocol import remaining

PREVIEW_NAMES = ("Metadata/plate_1.png", "Metadata/top_1.png", "Metadata/pick_1.png")


def inject_source(root: Path, deadline: int):
    source = root / "sliced.3mf"
    validate(source, "3mf", deadline)
    with zipfile.ZipFile(source) as archive:
        has_preview = any(name in archive.namelist() for name in PREVIEW_NAMES)
    if has_preview:
        shutil.copyfile(source, root / "checkpoint.3mf")
        return
    png = root / "source_png.png"
    if not png.exists() and (root / "mesh.stl").exists():
        from backend.app.services.stl_thumbnail import generate_stl_thumbnail

        generated = generate_stl_thumbnail(root / "mesh.stl", root)
        if generated:
            Path(generated).replace(root / "preview.png")
            png = root / "preview.png"
    if not png.exists():
        shutil.copyfile(source, root / "checkpoint.3mf")
        return
    validate(png, "png", deadline)
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(root / "checkpoint.3mf", "w", zipfile.ZIP_DEFLATED) as dst:
        for entry in src.infolist():
            remaining(deadline)
            with src.open(entry) as reader, dst.open(entry, "w") as writer:
                shutil.copyfileobj(reader, writer, 128 * 1024)
        data = png.read_bytes()
        for name in PREVIEW_NAMES:
            dst.writestr(name, data)
    validate(root / "checkpoint.3mf", "3mf", deadline)
