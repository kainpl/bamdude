"""Only services/product_variants.py writes a part's variant bindings (spec
product-part-variant-combinations, rule 3).

A part's bindings are its ``product_part_options`` rows — ``ProductPart.option_links``.
``set_part_options`` (a part's own door, behind the product gate, freezing the saved
lines first) and ``copy_part_options`` (a product copy) are the only writers. Anything
else that writes the rows is a second writer and fails here. The scan sees:

* Core: ``insert`` / ``update`` / ``delete`` of ``ProductPartOption``, its ``__table__``
  or the table named by its name (``table("product_part_options", …)``,
  ``metadata.tables["product_part_options"]``) — called with the table as the argument
  or as its method (``ProductPartOption.__table__.insert()``, the form
  ``routes/library_tags.py`` uses for its link table); raw SQL naming the table;
* the collection: assigning, deleting or augmenting ``.option_links``, assigning or
  deleting a slice of it (``part.option_links[:] = []``), a list mutator called on it or
  on a name bound to it, ``setattr(x, "option_links", …)``, a non-empty
  ``option_links=`` keyword;
* a link object: building a ``ProductPartOption``; and, for an object that comes from
  ``.option_links``, a ``ProductPartOption(...)`` or a query selecting the entity
  (``select(ProductPartOption)``, ``get(ProductPartOption, …)``) — followed through
  assignments, ``for`` loops and comprehensions within one function — passing it to
  ``add`` / ``add_all`` / ``delete`` / ``merge``, or writing its ``part_id`` /
  ``option_id`` / ``group_id`` (by assignment or ``setattr``).

The field rule is deliberately tied to link objects, not to the attribute name: other
models carry ``part_id`` / ``option_id`` / ``group_id`` too (line and position count and
choice rows), and their writers are ``line_config``'s.

Out of reach of a syntax scan (named so nobody mistakes the guard for a proof): a link
object reached through an indirection the scan does not follow — returned by another
function, stored on an attribute (``self.link``), passed as a parameter into a function
that writes it, or ``getattr(part, "option_links")``; a table name or SQL built at run
time (``f"DELETE FROM {name}"``); and ORM bulk APIs handed rows by such an indirection.
The scan is flow-insensitive within a function: a name bound to links anywhere in it
counts as links everywhere in it.

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
TABLE = "product_part_options"
COLLECTION = "option_links"
LINK_FIELDS = {"part_id", "option_id", "group_id"}
_STATEMENTS = {"insert", "update", "delete"}
_MUTATORS = {"append", "extend", "insert", "remove", "pop", "clear"}
_SESSION_WRITES = {"add", "add_all", "delete", "merge"}
# Calls whose result is (an iterable of) the objects their argument yields.
_PASS_THROUGH = {"list", "tuple", "set", "sorted", "reversed", "next", "iter", "filter", "zip", "enumerate"}
# Result methods of a query: ``(await db.execute(q)).scalars().all()`` yields what ``q`` selects.
_RESULT_METHODS = {
    "scalars",
    "scalar",
    "all",
    "first",
    "one",
    "one_or_none",
    "scalar_one",
    "scalar_one_or_none",
    "unique",
    "fetchall",
}
_ENTITY_LOOKUPS = {"select", "get", "query", "get_one"}
_RAW_WRITE = re.compile(rf"\b(insert\s+into|delete\s+from|update)\s+{TABLE}\b", re.IGNORECASE)


def _name(node: ast.AST) -> str | None:
    return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None


def _is_table_name(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == TABLE


def _names_the_model(node: ast.AST) -> bool:
    """The table, however it is reached: ``ProductPartOption``, ``ProductPartOption.__table__``,
    ``table("product_part_options", …)`` / ``Table(…)``, ``….tables["product_part_options"]``."""
    if isinstance(node, ast.Attribute) and node.attr == "__table__":
        node = node.value
    if isinstance(node, ast.Call) and _name(node.func) in {"table", "Table"}:
        return bool(node.args) and _is_table_name(node.args[0])
    if isinstance(node, ast.Subscript):
        return _is_table_name(node.slice)
    return _name(node) == MODEL


def _is_empty_list(node: ast.AST) -> bool:
    return isinstance(node, ast.List) and not node.elts


def _selects_entity(node: ast.AST) -> bool:
    """A query or lookup in ``node`` that returns link OBJECTS — ``select(ProductPartOption)``,
    ``db.get(ProductPartOption, key)`` — not columns of them (``select(ProductPartOption.part_id)``)."""
    return any(
        isinstance(sub, ast.Call)
        and _name(sub.func) in _ENTITY_LOOKUPS
        and any(isinstance(arg, ast.Name) and arg.id == MODEL for arg in sub.args)
        for sub in ast.walk(node)
    )


def _yields_links(node: ast.AST | None, links: set[str]) -> bool:
    """Is ``node`` a link object, or a collection / query result of them? ``links`` are the
    names already known to hold them in this function."""
    if node is None:
        return False
    if isinstance(node, (ast.Await, ast.Starred)):
        return _yields_links(node.value, links)
    if isinstance(node, ast.Name):
        return node.id in links
    if isinstance(node, ast.Attribute):
        return node.attr == COLLECTION
    if isinstance(node, ast.Subscript):
        return _yields_links(node.value, links)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return any(_yields_links(element, links) for element in node.elts)
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        inner = links | {
            name
            for generator in node.generators
            if _yields_links(generator.iter, links)
            for name in _bound_names(generator.target)
        }
        return _yields_links(node.elt, inner)
    if isinstance(node, ast.BinOp):
        return _yields_links(node.left, links) or _yields_links(node.right, links)
    if isinstance(node, ast.BoolOp):
        return any(_yields_links(value, links) for value in node.values)
    if isinstance(node, ast.IfExp):
        return _yields_links(node.body, links) or _yields_links(node.orelse, links)
    if isinstance(node, ast.Call):
        name = _name(node.func)
        if name == MODEL:
            return True
        if name in _PASS_THROUGH:
            return any(_yields_links(arg, links) for arg in node.args)
        if isinstance(node.func, ast.Attribute) and name in _RESULT_METHODS and _yields_links(node.func.value, links):
            return True
        return _selects_entity(node)
    return False


def _bound_names(target: ast.AST) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _bound_names(element)]
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    return []


def _binding(node: ast.AST) -> tuple[list[ast.AST], ast.AST | None]:
    """What ``node`` binds and from what: an assignment's targets and value, a ``for``
    loop's or a comprehension's target and iterable, a walrus."""
    if isinstance(node, ast.Assign):
        return list(node.targets), node.value
    if isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
        return [node.target], node.value
    if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        return [node.target], node.iter
    return [], None


def _link_names(nodes: list[ast.AST]) -> set[str]:
    """Every name bound to link objects in a function — assignments, ``for`` loops,
    comprehensions, walrus — to a fixpoint (a name bound from another such name counts)."""
    links: set[str] = set()
    while True:
        before = len(links)
        for node in nodes:
            targets, source = _binding(node)
            if targets and _yields_links(source, links):
                links.update(name for target in targets for name in _bound_names(target))
        if len(links) == before:
            return links


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


def _is_collection(node: ast.AST, links: set[str]) -> bool:
    """``.option_links`` itself, or a name bound to it (or to links) in this function."""
    return _name(node) == COLLECTION or (isinstance(node, ast.Name) and node.id in links)


def _call_writes(node: ast.Call, links: set[str]) -> bool:
    func, name = node.func, _name(node.func)
    builds = name == MODEL
    # insert(ProductPartOption) / delete(ProductPartOption.__table__) …
    statement = name in _STATEMENTS and bool(node.args) and _names_the_model(node.args[0])
    # … and ProductPartOption.__table__.insert() / .delete() / .update()
    method_statement = isinstance(func, ast.Attribute) and name in _STATEMENTS and _names_the_model(func.value)
    mutates = isinstance(func, ast.Attribute) and name in _MUTATORS and _is_collection(func.value, links)
    session_write = (
        isinstance(func, ast.Attribute)
        and name in _SESSION_WRITES
        and bool(node.args)
        and _yields_links(node.args[0], links)
    )
    sets = (
        name == "setattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and (
            node.args[1].value == COLLECTION
            or (node.args[1].value in LINK_FIELDS and _yields_links(node.args[0], links))
        )
    )
    keyword = any(kw.arg == COLLECTION and not _is_empty_list(kw.value) for kw in node.keywords)
    return builds or statement or method_statement or mutates or session_write or sets or keyword


def _target_writes(target: ast.AST, links: set[str]) -> bool:
    if isinstance(target, ast.Attribute):
        return target.attr == COLLECTION or (target.attr in LINK_FIELDS and _yields_links(target.value, links))
    if isinstance(target, ast.Subscript):  # part.option_links[:] = [] / del part.option_links[0]
        return _is_collection(target.value, links)
    return False


def _scopes(tree: ast.AST) -> list[list[ast.AST]]:
    """Each function's nodes (nested functions included, so a closure sees its outer
    names), and the module's own code outside every function."""
    functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    inside = {id(sub) for function in functions for sub in ast.walk(function)}
    module = [node for node in ast.walk(tree) if id(node) not in inside]
    return [module, *(list(ast.walk(function)) for function in functions)]


def _node_writes(node: ast.AST, links: set[str]) -> bool:
    if isinstance(node, ast.Call):
        return _call_writes(node, links)
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and bool(_RAW_WRITE.search(node.value))
    return any(_target_writes(target, links) for target in _targets(node))


def _writes(tree: ast.AST) -> list[int]:
    hits: set[int] = set()
    for nodes in _scopes(tree):
        links = _link_names(nodes)
        hits.update(node.lineno for node in nodes if _node_writes(node, links))
    return sorted(hits)


def test_nothing_but_the_variant_writer_writes_part_bindings():
    offenders = []
    for path in APP.rglob("*.py"):
        if path in ALLOWED or "migrations" in path.parts:
            continue
        for line in _writes(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.append(f"{path.relative_to(APP)}:{line}")
    assert offenders == []


# Each snippet's write is its LAST line, and nothing before it is a write.
_WRITERS = [
    # Core, the table as an argument
    "await db.execute(delete(ProductPartOption).where(ProductPartOption.part_id == 1))",
    "await db.execute(insert(ProductPartOption.__table__), rows)",
    "await db.execute(insert(table('product_part_options', column('part_id'))), rows)",
    # Core, the table's own method (``routes/library_tags.py`` writes its link table so)
    "await db.execute(ProductPartOption.__table__.insert(), rows)",
    "await db.execute(ProductPartOption.__table__.delete().where(ProductPartOption.part_id == 1))",
    "await db.execute(ProductPartOption.__table__.update().values(group_id=2))",
    "await db.execute(Base.metadata.tables['product_part_options'].delete())",
    # raw SQL
    "await db.execute(text('DELETE FROM product_part_options WHERE part_id = 1'))",
    # the collection
    "part.option_links = links",
    "del part.option_links",
    "part.option_links += links",
    "part.option_links[:] = []",
    "del part.option_links[0]",
    "part.option_links.append(link)",
    "part.option_links.clear()",
    "links = part.option_links\nlinks.remove(link)",
    "setattr(part, 'option_links', links)",
    "ProductPart(name='x', option_links=links)",
    # a link object
    "db.add(ProductPartOption(part_id=1, option_id=2, group_id=3))",
    "db.add_all(source.option_links)",
    "for link in part.option_links:\n    await db.delete(link)",
    "[await db.delete(link) for link in part.option_links]",
    "link = await db.get(ProductPartOption, (1, 2))\nawait db.delete(link)",
    "links = (await db.scalars(select(ProductPartOption).where(ProductPartOption.part_id == 1))).all()\n"
    "for link in links:\n"
    "    await db.merge(link)",
    "for link in source.option_links:\n    link.part_id = target.id",
    "link = part.option_links[0]\nlink.option_id = 7",
    "part.option_links[0].group_id = 4",
    "link = next(x for x in part.option_links if x.option_id == 1)\nsetattr(link, 'part_id', 9)",
    "async def merge(db, source, target):\n    for link in list(source.option_links):\n        link.part_id = target.id",
]


def test_the_scan_sees_a_writer_when_there_is_one():
    # A guard that could never fail proves nothing.
    for snippet in _WRITERS:
        assert _writes(ast.parse(snippet)) == [snippet.count("\n") + 1], snippet


def test_a_part_built_unbound_and_a_read_are_not_writes():
    """``option_links=[]`` at construction writes nothing; reading the links, the
    ``bound`` property and a query over the table's columns are reads; another model's
    ``part_id`` / ``option_id`` / ``group_id`` is that model's writer's business; a part's
    own deletion takes its links through the ORM cascade; and a name bound to links in
    one function says nothing about the same name in another."""
    for snippet in [
        "ProductPart(product_id=1, name='x', option_links=[])",
        "ids = [link.option_id for link in part.option_links]",
        "bound = part.bound",
        "select(ProductPartOption.part_id).where(ProductPartOption.option_id.in_(ids))",
        "option_links: Mapped[list['ProductPartOption']] = relationship(cascade='all, delete-orphan')",
        "seen = set()\nfor link in part.option_links:\n    seen.add(link.option_id)",
        "rows = (await db.execute(select(ProductPartOption.option_id))).all()\nfor (option_id,) in rows:\n"
        "    ids.add(option_id)",
        "count = ProjectLinePartCount(line_id=1, part_id=2, qty=3)\ncount.part_id = 5\ndb.add(count)",
        "db.add(ProjectLineChoice(line_id=1, group_id=2, option_id=3))",
        "row.part_id = target.id",
        "await db.delete(part)",
        "await db.execute(LibraryFileTag.__table__.insert(), rows)",
        "def one(part):\n    link = part.option_links[0]\n\nasync def two(db, link):\n    await db.delete(link)",
    ]:
        assert _writes(ast.parse(snippet)) == [], snippet
