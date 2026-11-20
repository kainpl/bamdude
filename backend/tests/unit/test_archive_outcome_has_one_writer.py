"""Only services/archive_outcome.py writes print_archives.outcome_checked_at / outcome_reminded_at.

The mark is «somebody looked at this print's result», first mark wins, and the reminder stamp is
what makes a reminder go out at most once. A second writer is how a door would start un-marking
prints, stamping reminders it never sent, or inventing a verdict beside the defect count.
"""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
ALLOWED = {APP / "services" / "archive_outcome.py"}
COLUMNS = {"outcome_checked_at", "outcome_reminded_at"}
#: Calls whose keyword arguments are column writes: the ORM constructor and Core ``.values()``.
_ROW_WRITERS = {"PrintArchive", "values"}


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _writes(tree: ast.AST) -> list[int]:
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        else:
            targets = []
        for target in targets:
            for leaf in ast.walk(target):
                if isinstance(leaf, ast.Attribute) and leaf.attr in COLUMNS:
                    hits.append(node.lineno)
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name == "setattr" and len(node.args) >= 2:
            key = node.args[1]
            if isinstance(key, ast.Constant) and key.value in COLUMNS:
                hits.append(node.lineno)
        elif name in _ROW_WRITERS:
            if any(kw.arg in COLUMNS for kw in node.keywords):
                hits.append(node.lineno)
            for arg in node.args:
                if isinstance(arg, ast.Dict) and any(
                    (isinstance(k, ast.Constant) and k.value in COLUMNS)
                    or (isinstance(k, ast.Attribute) and k.attr in COLUMNS)
                    for k in arg.keys
                ):
                    hits.append(node.lineno)
    return hits


def test_nothing_but_the_outcome_service_writes_the_marks():
    offenders = []
    for path in APP.rglob("*.py"):
        if path in ALLOWED or "migrations" in path.parts:
            continue
        for line in _writes(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.append(f"{path.relative_to(APP)}:{line}")
    assert offenders == []


def test_the_outcome_service_is_seen_as_a_writer():
    # The allowlisted file must really be where the writes are, or the allowlist guards nothing.
    (path,) = ALLOWED
    assert _writes(ast.parse(path.read_text(encoding="utf-8")))


def test_the_scan_sees_a_writer_when_there_is_one():
    # A guard that could never fail proves nothing.
    source = "\n".join(
        [
            "archive.outcome_checked_at = now",
            "archive.outcome_reminded_at = None",
            "setattr(archive, 'outcome_checked_at', now)",
            "db.add(PrintArchive(filename='f', outcome_checked_at=now))",
            "await db.execute(update(PrintArchive).values(outcome_reminded_at=now))",
            "await db.execute(update(PrintArchive).values({PrintArchive.outcome_checked_at: None}))",
            "row = {'outcome_checked_at': archive.outcome_checked_at}",  # a read into a response: not a write
            "outcome_checked_at: datetime | None = None",  # a schema or model field: not a write
            "Response(outcome_checked_at=archive.outcome_checked_at)",  # a reader's keyword: not a write
        ]
    )
    assert _writes(ast.parse(source)) == [1, 2, 3, 4, 5, 6]


def test_every_door_reaches_the_mark_through_the_writer():
    """The defects writer marks for every defects door; the explicit doors and the restore call the
    writer themselves. A new caller is a new door — name it here with its reason."""
    callers: dict[str, set[str]] = {}
    for path in APP.rglob("*.py"):
        if path in ALLOWED:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if isinstance(owner, ast.Name) and owner.id == "archive_outcome":
                callers.setdefault(node.func.attr, set()).add(path.relative_to(APP).as_posix())
    assert callers.get("mark_checked") == {"services/archive_defects.py"}
    assert callers.get("mark_checked_explicitly") == {
        "api/routes/archives.py",
        "services/telegram_handlers/defects.py",
    }
    assert callers.get("forget") == {"services/dispatch_phase.py"}
    # A plate the swap changed by itself: our macro answered, or the file's own sequence cleared it.
    assert callers.get("mark_checked_by_swap") == {"services/swap_terminal.py"}
    assert callers.get("carry_restored") == {"services/git_restore.py"}
    assert callers.get("remind_due") == {"services/notification_service.py"}
