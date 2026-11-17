"""Conservative C11 locators, including imported DELETE aliases and folder cascades.

This is a coverage census, not inferred deletion authority. Dynamic Python
edges still require the explicit owner adjudication recorded in the fixture.
"""

import ast
import re
from pathlib import Path

PRIMITIVES = {
    "trash_or_purge",
    "hard_delete_now",
    "purge_rows",
    "library_rows",
    "archive_rows",
    "folder_rows",
    "delete_archive_rows",
    "purge_file_product_links",
    "forget_files",
    "delete_archive",
    "detach_print_queue_refs",
    "detach_auto_queue_refs",
}


def spell(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return spell(node.value) + "." + node.attr
    return ""


def census(sources):
    deletes, primitives, edges, definitions = set(), set(), set(), set()
    for path, source in sorted(sources.items()):
        module = "backend.app." + path.removesuffix(".py").replace("/", ".")
        tree = ast.parse(source)
        bindings = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    bindings[alias.asname or alias.name] = node.module + "." + alias.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    bindings[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0]
                    )

        def resolve(name, bindings=bindings):
            head, *tail = name.split(".")
            return ".".join([bindings.get(head, head), *tail])

        # Imported aliases and assignment aliases are equally executable doors.
        # Follow their finite static chains, including named callback aliases.
        assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)]
        for _ in range(len(assignments)):
            changed = False
            for assignment in assignments:
                value = spell(assignment.value)
                if not value:
                    continue
                target = resolve(value)
                if target.split(".")[-1] not in {"delete", *PRIMITIVES}:
                    continue
                for name in assignment.targets:
                    if isinstance(name, ast.Name) and name.id != value and bindings.get(name.id) != target:
                        bindings[name.id] = target
                        changed = True
            if not changed:
                break

        class Visitor(ast.NodeVisitor):
            def __init__(self, path=path, module=module, bindings=bindings, resolve=resolve):
                self.path, self.module, self.bindings, self.resolve = path, module, bindings, resolve
                self.stack = []
                self.classes = []
                self.function = []

            @property
            def owner(self):
                return self.path + "::" + ".".join(self.stack)

            def visit_ClassDef(self, node):
                self.stack.append(node.name)
                self.classes.append(node.name)
                self.generic_visit(node)
                self.classes.pop()
                self.stack.pop()

            def visit_FunctionDef(self, node):
                self.stack.append(node.name)
                self.function.append(node)
                definitions.add((self.module + "." + ".".join(self.stack), self.owner))
                for statement in node.body:
                    # A docstring mentioning DELETE is not executable SQL.
                    if (
                        statement is node.body[0]
                        and isinstance(statement, ast.Expr)
                        and isinstance(statement.value, ast.Constant)
                        and isinstance(statement.value.value, str)
                    ):
                        continue
                    self.visit(statement)
                self.function.pop()
                self.stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node):
                name = spell(node.func)
                target = self.resolve(name)
                if self.function:
                    if target.endswith(".delete") or name == "delete":
                        argument = (
                            self.resolve(spell(node.args[0]))
                            if node.args and spell(node.args[0])
                            else ast.dump(node.args[0], include_attributes=False)
                            if node.args
                            else ""
                        )
                        deletes.add((self.owner, target, argument))
                    if target.split(".")[-1] in PRIMITIVES:
                        primitives.add((self.owner, target))
                    if name.startswith("self.") and self.classes:
                        target = self.module + "." + ".".join(self.classes) + "." + name.split(".", 1)[1]
                    elif isinstance(node.func, ast.Name) and node.func.id not in self.bindings:
                        target = self.module + "." + node.func.id
                    edges.add((self.owner, target))
                    for argument in [*node.args, *[keyword.value for keyword in node.keywords]]:
                        if isinstance(argument, (ast.Name, ast.Attribute)):
                            target_arg = self.resolve(spell(argument))
                            if isinstance(argument, ast.Name) and argument.id not in self.bindings:
                                target_arg = self.module + "." + argument.id
                            edges.add((self.owner, target_arg))
                self.generic_visit(node)

            def visit_Constant(self, node):
                if self.function and isinstance(node.value, str) and re.search(r"\bDELETE\s+FROM\b", node.value, re.I):
                    normalized = " ".join(node.value.split())
                    deletes.add((self.owner, "raw SQL", normalized))

        Visitor().visit(tree)
    # Fixed point, not a depth cap: direct/transitive callers and named callbacks
    # through every statically resolvable owning function. Nested/late-bound
    # callbacks remain visible primitive sites even without a resolved edge.
    by_target = dict(definitions)
    owning = {owner for owner, _callee in primitives}
    while True:
        added = {owner for owner, target in edges if by_target.get(target) in owning}
        if added <= owning:
            break
        owning.update(added)
    caller_edges = {(owner, target) for owner, target in edges if by_target.get(target) in owning}
    return {
        "delete_sites": sorted(deletes),
        "primitive_sites": sorted(primitives),
        "caller_edges": sorted(caller_edges),
    }


def application_sources(root: Path):
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.py"))
        if "migrations" not in path.relative_to(root).parts and "__pycache__" not in path.parts
    }
