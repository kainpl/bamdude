"""N62 §9.3 (r1 B02): a key's or a chat's stored printer list is read by ``core/printer_scope``.

``APIKey.printer_ids`` and ``TelegramChat.printer_ids`` are only part of an answer: a key is held
to its list ∩ its owner's scope (D5), a chat to its list ∩ its group's reach (D4). A reader that
went to the column directly would miss the narrowing — so outside the scope module only the places
that WRITE the list (the key and chat routes, a deleted printer's clean-up), the models and the
frozen printer-number registry may touch it.

The walk is by name: an attribute ``printer_ids`` read off something named like a key or a chat
(``api_key``, ``key``, ``chat``, ``self`` inside the two models) or off the classes themselves.
Other ``printer_ids`` — a notification provider's, a request body's, a diagnostic access — are
not these lists.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).parents[2] / "app"

ALLOWED = {
    "core/printer_scope.py": "the one reader (key_list, chat_list)",
    "api/routes/api_keys.py": "writes a key's list",
    "api/routes/telegram.py": "writes a chat's list",
    "api/routes/printers.py": "a deleted printer leaves every key's list (the writer of the clean-up)",
    "models/api_key.py": "the column",
    "models/telegram_chat.py": "the column and its own projection, which printer_scope.chat_scope narrows",
    "core/printer_id_bound.py": "the frozen registry of every place a printer number lives",
}

_KEYISH = ("key", "chat")


def _base_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _offenders() -> list[str]:
    out = []
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        if rel in ALLOWED or rel.startswith(("migrations/", "schemas/")):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Attribute) and node.attr == "printer_ids"):
                continue
            base = _base_name(node.value) or ""
            if base in {"APIKey", "TelegramChat"} or any(word in base.lower() for word in _KEYISH):
                out.append(f"{rel}:{node.lineno} {ast.unparse(node)}")
    return out


def test_only_the_scope_reads_a_key_or_chat_printer_list():
    offenders = _offenders()
    assert not offenders, (
        "Read a key's or a chat's printers through core/printer_scope (key_scope / chat_scope; "
        "key_list / chat_list for the list alone), or add the file to ALLOWED with the reason: " + repr(offenders)
    )


def test_the_walk_finds_the_readers_it_allows(tmp_path):
    """The walk is not vacuous: the scope module itself reads ``api_key.printer_ids``."""
    tree = ast.parse((APP / "core/printer_scope.py").read_text(encoding="utf-8"))
    found = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "printer_ids"]
    assert "api_key.printer_ids" in found and "chat.printer_ids" in found
