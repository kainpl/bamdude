"""Nothing but services/part_images.py writes a part's picture choice or its photos (spec §15; plan E4)."""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
COLUMNS = {"image_source", "image_file_id", "image_plate_index", "image_identify_id", "image_photo"}
ALLOWED = {APP / "services" / "part_images.py", APP / "models" / "product.py"}
FOLDER_ALLOWED = {APP / "services" / "part_images.py", APP / "services" / "product_files.py"}


def _writes(tree: ast.AST) -> list[int]:
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(s, ast.Attribute) and s.attr in COLUMNS for t in targets for s in ast.walk(t)):
                hits.append(node.lineno)
        elif isinstance(node, ast.Call):
            if any(kw.arg in COLUMNS for kw in node.keywords):
                hits.append(node.lineno)  # ProductPart(image_source=...), .values(image_photo=...)
            elif (
                isinstance(node.func, ast.Name)
                and node.func.id == "setattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in COLUMNS
            ):
                hits.append(node.lineno)
        elif isinstance(node, ast.Dict) and any(isinstance(k, ast.Constant) and k.value in COLUMNS for k in node.keys):
            hits.append(node.lineno)
    return hits


def _sources():
    for path in APP.rglob("*.py"):
        if "migrations" not in path.parts:
            yield path, path.read_text(encoding="utf-8")


def test_nothing_but_the_writer_writes_the_choice():
    offenders = [
        f"{path.relative_to(APP)}:{line}"
        for path, source in _sources()
        if path not in ALLOWED
        for line in _writes(ast.parse(source))
    ]
    assert offenders == []


def test_nothing_but_the_writer_names_the_photo_folder():
    offenders = [
        str(path.relative_to(APP))
        for path, source in _sources()
        if '"part-images"' in source and path not in FOLDER_ALLOWED
    ]
    assert offenders == []


def test_the_part_bodies_carry_no_choice():
    """update_part writes every field of its body through setattr -- the scan above cannot see a name in a variable."""
    from backend.app.schemas.product import ProductPartCreate, ProductPartUpdate

    assert not (set(ProductPartCreate.model_fields) | set(ProductPartUpdate.model_fields)) & COLUMNS


def test_the_scan_sees_a_writer_when_there_is_one():
    tree = ast.parse(
        "part.image_source = 'auto'\n"
        "db.add(ProductPart(image_photo='x'))\n"
        "await db.execute(update(ProductPart).values(image_file_id=1))\n"
        "setattr(part, 'image_identify_id', 3)\n"
        "rows.append({'image_plate_index': 1})\n"
    )
    assert sorted(_writes(tree)) == [1, 2, 3, 4, 5]  # ast.walk is breadth-first, not line order
