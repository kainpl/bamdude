"""A new upload door cannot silently become installation-owned deduplication."""

import ast
from pathlib import Path


def test_every_request_upload_carries_its_actor_into_the_shared_writer():
    root = Path(__file__).resolve().parents[2] / "app"
    seen = set()
    for source in root.rglob("*.py"):
        for call in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                continue
            if call.func.id not in {"save_3mf_bytes_to_library", "store_library_upload"}:
                continue
            assert "authority" in {argument.arg for argument in call.keywords}, (
                f"{source.relative_to(root)}:{call.lineno}: carry the request actor; None is not an API-key identity"
            )
            seen.add(source.relative_to(root).as_posix())
    assert {
        "api/routes/archives.py",
        "api/routes/printers.py",
        "api/routes/makerworld.py",
        "api/routes/library.py",
        "services/product_card.py",
        "services/telegram_handlers/library_upload_scene.py",
    } <= seen


def test_the_global_dedup_decision_always_receives_a_library_read_policy():
    root = Path(__file__).resolve().parents[2] / "app"
    seen = 0
    for source in root.rglob("*.py"):
        for call in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "find_reusable_row":
                assert "access" in {argument.arg for argument in call.keywords}, f"{source}:{call.lineno}"
                seen += 1
    assert seen >= 8
