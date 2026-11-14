"""Only services/product_variants.py writes a part's variant bindings (spec
product-part-variant-combinations, rule 3).

A part's bindings are its ``product_part_options`` rows — ``ProductPart.option_links``.
``set_part_options`` (a part's own door, behind the product gate, freezing the saved
lines first) and ``copy_part_options`` (a product copy) are the only writers. Anything
else that builds a ``ProductPartOption``, assigns or mutates ``.option_links``, or
inserts / updates / deletes the table — through the ORM, Core or raw SQL — is a second
writer and fails here.

The one thing allowed elsewhere is constructing a part with ``option_links=[]``: it
writes nothing, and it is what keeps a part flushed by another door readable under
async SQLAlchemy (``ProductPart.bound`` on a never-loaded collection is a
``MissingGreenlet``). A part's deletion drops its rows through the ORM cascade of
``ProductPart.option_links`` — the part's end, not a binding write. Migrations are
excluded (m225 moved the old column into the table).
"""

import ast
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
ALLOWED = {APP / "services" / "product_variants.py", APP / "models" / "product_variant.py"}
MODEL = "ProductPartOption"
COLLECTION = "option_links"
_MUTATORS = {"append", "extend", "insert", "remove", "pop", "clear"}
_RAW_WRITE = re.compile(r"\b(insert\s+into|delete\s+from|update)\s+product_part_options\b", re.IGNORECASE)


def _name(node: ast.AST) -> str | None:
    return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None


def _names_the_model(node: ast.AST) -> bool:
    """``ProductPartOption`` or ``ProductPartOption.__table__``."""
    if isinstance(node, ast.Attribute) and node.attr == "__table__":
        node = node.value
    return _name(node) == MODEL


def _is_empty_list(node: ast.AST) -> bool:
    return isinstance(node, ast.List) and not node.elts


def _targets(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.Assign):
        found = list(node.targets)
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        found = [node.target]
    elif isinstance(node, ast.Delete):
        found = list(node.targets)
    else:
        return []
    flat: list[ast.AST] = []
    while found:
        target = found.pop()
        if isinstance(target, (ast.Tuple, ast.List)):
            found.extend(target.elts)
        else:
            flat.append(target)
    return flat


def _call_writes(node: ast.Call) -> bool:
    """``ProductPartOption(...)``; ``insert`` / ``update`` / ``delete`` of the model or its
    table; a mutator called on ``.option_links``; ``setattr(x, "option_links", ...)``."""
    func, name = node.func, _name(node.func)
    builds = name == MODEL
    statement = name in {"insert", "update", "delete"} and bool(node.args) and _names_the_model(node.args[0])
    mutates = name in _MUTATORS and isinstance(func, ast.Attribute) and _name(func.value) == COLLECTION
    sets = (
        name == "setattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == COLLECTION
    )
    return builds or statement or mutates or sets


def _writes(tree: ast.AST) -> list[int]:
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if _call_writes(node):
                hits.append(node.lineno)
            hits.extend(node.lineno for kw in node.keywords if kw.arg == COLLECTION and not _is_empty_list(kw.value))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _RAW_WRITE.search(node.value):
            hits.append(node.lineno)
        hits.extend(
            node.lineno for target in _targets(node) if isinstance(target, ast.Attribute) and target.attr == COLLECTION
        )
    return sorted(hits)


def test_nothing_but_the_variant_writer_writes_part_bindings():
    offenders = []
    for path in APP.rglob("*.py"):
        if path in ALLOWED or "migrations" in path.parts:
            continue
        for line in _writes(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.append(f"{path.relative_to(APP)}:{line}")
    assert offenders == []


def test_the_scan_sees_a_writer_when_there_is_one():
    # A guard that could never fail proves nothing.
    tree = ast.parse(
        "\n".join(
            [
                "db.add(ProductPartOption(part_id=1, option_id=2, group_id=3))",
                "await db.execute(delete(ProductPartOption).where(ProductPartOption.part_id == 1))",
                "await db.execute(insert(ProductPartOption.__table__), rows)",
                "part.option_links = links",
                "part.option_links.append(link)",
                "part.option_links.clear()",
                "setattr(part, 'option_links', links)",
                "ProductPart(name='x', option_links=links)",
                "await db.execute(text('DELETE FROM product_part_options WHERE part_id = 1'))",
                "del part.option_links",
                "part.option_links += links",
            ]
        )
    )
    assert _writes(tree) == list(range(1, 12))


def test_a_part_built_unbound_and_a_read_are_not_writes():
    """``option_links=[]`` at construction writes nothing; reading the links, the
    ``bound`` property and a query over the table are reads."""
    tree = ast.parse(
        "\n".join(
            [
                "ProductPart(product_id=1, name='x', option_links=[])",
                "ids = [link.option_id for link in part.option_links]",
                "bound = part.bound",
                "select(ProductPartOption.part_id).where(ProductPartOption.option_id.in_(ids))",
                "option_links: Mapped[list['ProductPartOption']] = relationship(cascade='all, delete-orphan')",
            ]
        )
    )
    assert _writes(tree) == []
