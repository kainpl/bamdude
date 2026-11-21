"""The checks of the queue-writer guard (G03 B, A13), over the scanner's sites and ``classification.json``.

Every function the scanner reports has an entry. Besides the human record (``class``, ``owner``, ``rule``,
``r3``, ``fk``) an entry carries the machine fields the checks read:

``lock``
    The lock class of the code AS IT IS: ``lane``, ``L2a`` (header only), ``L2b`` (one-row statement),
    ``L2c`` (startup only), ``L2c-runtime`` (a lookup that writes only a missing header), ``L2d`` (archive-guarded
    last lock), ``L2e`` (skip-locked back-fill), ``shared`` (AutoQueue rows: one, or several prelocked ascending),
    ``shared-new`` (new AutoQueue rows), ``helper`` (runs in its caller's class), ``memory`` (no durable write),
    ``n/a`` (the receiver is another object), ``migration``, or ``pending`` — a function whose class the spec
    changes (``class`` names it after ``->``) and whose package has not landed yet. The package that makes the
    code change sets ``lock`` to the new class in the same change, so the list never names a class the code
    does not have.
``via`` (``lane``)
    Helpers, ``module.function``, whose call sites stand inside the lane because the callee takes it; the check
    asks that the callee's own function enters a lane scope.
``outside`` (``lane``)
    ``{signature: reason}`` — sites that lie outside the lane by design (the recount after the scope is ``L2a``;
    an AutoQueue row is ``shared``; a closer whose own lane is pending).
``after_commit`` (``lane``)
    ``{signature: reason}`` — sites the scan finds after a ``commit()`` inside the lane's block. The scan is
    path-insensitive; an entry records a case read in the code (the commit ends its branch in ``return``) or a
    pending change.
``candidates``
    ``{candidate key: "target" | "other: …"}`` — every site whose receiver no annotation or query types, read by
    hand: a real write of a queue row, or a write of another object (a response, an archive, a job). A new
    untyped site fails until it is read.

A site's *signature* is ``kind:model-or-helper[columns]``; a candidate key adds ``@receiver``.
"""

from __future__ import annotations

import ast
import json
from collections import defaultdict
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from backend.tests.queue_writers import writer_inventory as wi

HERE = Path(__file__).resolve().parent
APP = HERE.parents[1] / "app"
CLASSIFICATION = HERE / "classification.json"

LOCKS = {
    "lane",
    "L2a",
    "L2b",
    "L2c",
    "L2c-runtime",
    "L2d",
    "L2e",
    "shared",
    "shared-new",
    "helper",
    "memory",
    "n/a",
    "migration",
    "pending",
}
LANE_SCOPES = set(wi.LANE_SCOPES)
QUEUE_TABLES = set(wi.TARGETS.values())
# The columns that decide which pending row of a queue prints next (A15).
HEAD_COLUMNS = {"status", "manual_start", "scheduled_time", "position", "queue_id"}
SETTING_KINDS = {"orm-attr", "setattr", "core-update", "core-insert", "construct"}


def signature(site) -> str:
    what = site.model or site.helper
    columns = f"[{','.join(sorted(site.columns))}]" if site.columns else ""
    return f"{site.kind}:{what}{columns}"


def candidate_key(site) -> str:
    return f"{signature(site)}@{site.receiver}"


def key(site) -> str:
    return f"{site.file}::{site.func}"


@dataclass(frozen=True)
class Scan:
    sites: tuple
    mods: dict
    by_rel: dict

    def functions(self) -> dict[str, list]:
        grouped = defaultdict(list)
        for site in self.sites:
            grouped[key(site)].append(site)
        return grouped

    def function_node(self, file: str, qualname: str):
        return self.by_rel[file].functions[qualname]


@cache
def scan() -> Scan:
    # The classification's ``_confirmed_candidate_writers`` make those functions writer helpers, so their call
    # sites are sites: the scan reads the file it is checked against.
    return scan_of(APP, classification())


def scan_of(app: Path, data: dict) -> Scan:
    sites, _writers, _transitive, mods = wi.inventory(app, data, paths=False)
    return Scan(tuple(sites), mods, {info.rel: info for info in mods.values()})


def classification() -> dict:
    return json.loads(CLASSIFICATION.read_text(encoding="utf-8"))


def entries(data: dict | None = None) -> dict:
    data = classification() if data is None else data
    return {k: v for k, v in data.items() if not k.startswith("_")}


# ---------------------------------------------------------------- lane cover


def _is_lane_call(expr) -> bool:
    return isinstance(expr, ast.Call) and wi._call_name(expr) in LANE_SCOPES


def _lane_item(expr) -> bool:
    """A ``with`` item that enters a lane: the scope's call, or a conditional whose one arm is that call (the
    direct claim's ``nullcontext() if lane_held else queue_claim_scope(...)``: the other arm means the caller
    holds it — blind spot 5)."""
    if _is_lane_call(expr):
        return True
    return isinstance(expr, ast.IfExp) and (_is_lane_call(expr.body) or _is_lane_call(expr.orelse))


def _guard_helpers(fn) -> set[str]:
    """Nested functions of ``fn`` that enter the scope they are handed -- ``enter_guard(scope)`` in the plate
    answer, which enters it on the function's ``AsyncExitStack`` (``enter_async_context(scope)``)."""
    names = set()
    for sub in ast.walk(fn):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub is not fn and sub.args.args:
            param = sub.args.args[0].arg
            if any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "enter_async_context"
                and call.args
                and isinstance(call.args[0], ast.Name)
                and call.args[0].id == param
                for call in ast.walk(sub)
            ):
                names.add(sub.name)
    return names


def _stack_entries(scope_node, before: int, helpers=frozenset()) -> list[int]:
    return [
        sub.lineno
        for sub in ast.walk(scope_node)
        if isinstance(sub, ast.Call)
        and (
            (isinstance(sub.func, ast.Attribute) and sub.func.attr == "enter_async_context")
            or (isinstance(sub.func, ast.Name) and sub.func.id in helpers)
        )
        and sub.args
        and _is_lane_call(sub.args[0])
        and sub.lineno < before
    ]


def _commits_between(scope_node, start: int, end: int) -> bool:
    return any(
        isinstance(sub, ast.Call) and wi._call_name(sub) == "commit" and start <= sub.lineno < end
        for sub in ast.walk(scope_node)
    )


def lane_cover(scan_: Scan, site) -> tuple[bool, bool]:
    """(inside a lane, after a commit inside it) for one site, read from the function's own syntax."""
    fn = scan_.function_node(site.file, site.func)
    parents = wi.parents_of(fn)
    at_line = [n for n in ast.walk(fn) if getattr(n, "lineno", None) == site.line and n is not fn]
    if not at_line:
        return False, False
    node = at_line[0]
    helpers = _guard_helpers(fn)
    cur = node
    while cur in parents and cur is not fn:
        cur = parents[cur]
        if isinstance(cur, (ast.With, ast.AsyncWith)):
            if any(_lane_item(item.context_expr) for item in cur.items):
                return True, _commits_between(cur, cur.lineno, site.line)
            if any(
                isinstance(item.context_expr, ast.Call) and wi._call_name(item.context_expr) == "AsyncExitStack"
                for item in cur.items
            ):
                entered = _stack_entries(cur, site.line, helpers)
                if entered:
                    return True, _commits_between(cur, min(entered), site.line)
    entered = _stack_entries(fn, site.line, helpers)
    if entered:
        # A stack passed in (``_take_guards(db, guards, ...)``): the lane entered earlier in this function.
        return True, _commits_between(fn, min(entered), site.line)
    return False, False


def enters_a_lane(scan_: Scan, dotted: str) -> bool:
    """Whether ``module.function`` (as a helper call names it) enters a lane scope in its own body."""
    module, _, qualname = dotted.partition(".")
    for info in scan_.mods.values():
        if info.name.rsplit(".", 1)[-1] != module or qualname not in info.functions:
            continue
        fn = info.functions[qualname]
        for sub in ast.walk(fn):
            if isinstance(sub, (ast.With, ast.AsyncWith)) and any(_lane_item(i.context_expr) for i in sub.items):
                return True
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "enter_async_context"
                and sub.args
                and _is_lane_call(sub.args[0])
            ):
                return True
    return False


# ---------------------------------------------------------------- the rules


def unclassified_and_stale(scan_: Scan, data: dict) -> tuple[list[str], list[str]]:
    reported = set(scan_.functions())
    known = set(entries(data))
    return sorted(reported - known), sorted(known - reported - memory_entries_kept(scan_, data))


def _function_of(scan_: Scan, name: str):
    file, _, qualname = name.partition("::")
    info = scan_.by_rel.get(file)
    return info.functions.get(qualname) if info is not None else None


def sets_committed_values(fn) -> bool:
    return any(isinstance(node, ast.Call) and wi._call_name(node) == "set_committed_value" for node in ast.walk(fn))


def memory_entries_kept(scan_: Scan, data: dict) -> set[str]:
    """The ``memory`` entries (A23) the scan rightly does not report: their function exists and writes its back-fill
    with ``set_committed_value`` -- no site the scan sees -- so the entry still names a real reader."""
    return {
        name
        for name, entry in entries(data).items()
        if entry.get("lock") == "memory"
        and name not in scan_.functions()
        and (fn := _function_of(scan_, name)) is not None
        and sets_committed_values(fn)
    }


def memory_functions_write_nothing(scan_: Scan, data: dict) -> list[str]:
    """A23 / A13's ``memory`` class: a value a reader derives for its own reasoning is set with
    ``set_committed_value``, never left a pending ORM change -- so a function classed ``memory`` has no site at all,
    and a plain attribute write there (a back-fill another writer's flush would carry) fails here."""
    funcs = scan_.functions()
    return [
        f"{name}:{site.line} {signature(site)} is a durable write in a memory function"
        for name, entry in sorted(entries(data).items())
        if entry.get("lock") == "memory"
        for site in funcs.get(name, ())
    ]


def bad_locks(data: dict) -> list[str]:
    return sorted(k for k, v in entries(data).items() if v.get("lock") not in LOCKS)


def lane_violations(scan_: Scan, data: dict) -> list[str]:
    out = []
    for name, sites in sorted(scan_.functions().items()):
        entry = entries(data).get(name, {})
        if entry.get("lock") != "lane":
            continue
        via = set(entry.get("via", []))
        outside = entry.get("outside", {})
        late = entry.get("after_commit", {})
        for site in sites:
            sig = signature(site)
            if site.kind == "helper-call" and site.helper in via:
                continue
            inside, after = lane_cover(scan_, site)
            if not inside and sig not in outside:
                out.append(f"{name}:{site.line} {sig} lies outside its lane")
            elif inside and after and sig not in late:
                out.append(f"{name}:{site.line} {sig} follows a commit inside its lane")
        for helper in sorted(via):
            if not enters_a_lane(scan_, helper):
                out.append(f"{name}: via {helper}, which enters no lane")
    return out


def stale_exemptions(scan_: Scan, data: dict) -> list[str]:
    """An exemption that names no site of its function (the code moved; the record must follow)."""
    out = []
    functions = scan_.functions()
    for name, entry in sorted(entries(data).items()):
        sigs = {signature(s) for s in functions.get(name, [])}
        helpers = {s.helper for s in functions.get(name, []) if s.kind == "helper-call"}
        cands = {candidate_key(s) for s in functions.get(name, []) if s.confidence == "candidate"}
        for field in ("outside", "after_commit", "unaddressed_locks"):
            out += [f"{name}: {field} {sig}" for sig in entry.get(field, {}) if sig not in sigs]
        out += [f"{name}: via {h}" for h in entry.get("via", []) if h not in helpers]
        out += [f"{name}: candidates {c}" for c in entry.get("candidates", {}) if c not in cands]
    return out


def unread_candidates(scan_: Scan, data: dict) -> list[str]:
    out = []
    for name, sites in sorted(scan_.functions().items()):
        read = entries(data).get(name, {}).get("candidates", {})
        for site in sites:
            if site.confidence == "candidate" and candidate_key(site) not in read:
                out.append(f"{name}:{site.line} {candidate_key(site)}")
    return out


def confirmed_writers_mismatch(scan_: Scan, data: dict) -> list[str]:
    """``_confirmed_candidate_writers`` makes a function a writer helper, so its call sites are sites. Every
    function with an untyped site read as a real write of a queue row is listed, and every listed function
    writes one (through such a site, or through a typed one)."""
    real = {k for k, v in entries(data).items() if "target" in v.get("candidates", {}).values()}
    typed = {key(s) for s in scan_.sites if s.confidence == "certain" and s.kind in SETTING_KINDS | {"session-delete"}}
    listed = set(data.get("_confirmed_candidate_writers", []))
    return [f"listed, writes no queue row: {k}" for k in sorted(listed - real - typed)] + [
        f"target candidate, not listed: {k}" for k in sorted(real - listed)
    ]


def _is_other(entry: dict, site) -> bool:
    return site.confidence == "candidate" and entry.get("candidates", {}).get(candidate_key(site), "").startswith(
        "other"
    )


def head_writes_without_a_lane(scan_: Scan, data: dict) -> list[str]:
    """A15: a lane-less writer never writes a column that decides a queue's next row. ``L2c`` runs at start-up,
    before any lane holder exists, and is not lane-less in that sense."""
    out = []
    lane_less = {"L2a", "L2b", "L2d", "L2e", "shared", "shared-new"}
    for name, sites in sorted(scan_.functions().items()):
        entry = entries(data).get(name, {})
        if entry.get("lock") not in lane_less:
            continue
        for site in sites:
            if (
                site.kind not in SETTING_KINDS
                or "PrintQueueItem" not in site.model.split("|")
                or _is_other(entry, site)
            ):
                continue
            hit = sorted(HEAD_COLUMNS & set(site.columns)) or (["**"] if "**" in site.columns else [])
            if hit:
                out.append(f"{name}:{site.line} {signature(site)} writes {', '.join(hit)} without the lane")
    return out


def job_rows_built_without_a_lane(scan_: Scan, data: dict) -> list[str]:
    """A15: no lane-less tail insert remains — a ``PrintQueueItem`` is built only by a lane holder or a helper
    of one (a pending function keeps its old shape until its package lands)."""
    allowed = {"lane", "helper", "pending", "migration"}
    out = []
    for name, sites in sorted(scan_.functions().items()):
        lock = entries(data).get(name, {}).get("lock")
        for site in sites:
            if site.kind == "construct" and site.model == "PrintQueueItem" and lock not in allowed:
                out.append(f"{name}:{site.line} builds a job row as {lock}")
    return out


# ---------------------------------------------------------------- foreign keys (L6, A24)


def _fk_of(call: ast.Call):
    for sub in ast.walk(call):
        if (
            isinstance(sub, ast.Call)
            and wi._call_name(sub) == "ForeignKey"
            and sub.args
            and isinstance(sub.args[0], ast.Constant)
        ):
            return str(sub.args[0].value)
    return None


@cache
def queue_foreign_keys() -> dict[str, frozenset[str]]:
    """The attributes of the three queue tables that are foreign keys, read from the models' syntax."""
    found = defaultdict(set)
    for path in sorted((APP / "models").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef):
                continue
            table = next(
                (
                    st.value.value
                    for st in node.body
                    if isinstance(st, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in st.targets)
                    and isinstance(st.value, ast.Constant)
                ),
                None,
            )
            if table not in QUEUE_TABLES:
                continue
            for st in node.body:
                if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and isinstance(st.value, ast.Call):
                    if wi._call_name(st.value) in {"mapped_column", "Column"} and _fk_of(st.value):
                        found[table].add(st.target.id)
    return {t: frozenset(c) for t, c in found.items()}


def fk_setters(scan_: Scan) -> dict[str, set[str]]:
    fks = queue_foreign_keys()
    out = defaultdict(set)
    for site in scan_.sites:
        if site.kind not in SETTING_KINDS:
            continue
        for model in site.model.split("|"):
            table = wi.TARGETS.get(model)
            if not table:
                continue
            hit = set(site.columns) & fks.get(table, frozenset())
            if site.kind == "construct" and "**" in site.columns:
                hit.add("** (any FK)")
            out[key(site)] |= {f"{table}.{column}" for column in hit}
    return {k: v for k, v in out.items() if v}


def fk_unrecorded(scan_: Scan, data: dict) -> list[str]:
    """Every function that sets a foreign key of a queue table names, per column, how its parent is held."""
    out = []
    for name, columns in sorted(fk_setters(scan_).items()):
        recorded = entries(data).get(name, {}).get("fk") or {}
        out += [f"{name}: {column}" for column in sorted(columns) if column not in recorded]
    return out


# ---------------------------------------------------------------- L6(b): explicit locks are NO KEY UPDATE


def _is_true(node) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _lock_calls(fn, line: int) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and node.lineno == line
        and isinstance(node.func, ast.Attribute)
        and (
            node.func.attr == "with_for_update"
            or (node.func.attr == "get" and any(k.arg == "with_for_update" for k in node.keywords))
        )
    ]


def _no_key_update(call: ast.Call) -> bool:
    """``with_for_update(key_share=True)`` (NO KEY UPDATE), its read forms, or ``db.get(…, with_for_update={"key_share":
    True})``."""
    if call.func.attr == "with_for_update":
        return any(k.arg in {"key_share", "read"} and _is_true(k.value) for k in call.keywords)
    value = next(k.value for k in call.keywords if k.arg == "with_for_update")
    return isinstance(value, ast.Dict) and any(
        isinstance(k, ast.Constant) and k.value in {"key_share", "read"} and _is_true(v)
        for k, v in zip(value.keys, value.values, strict=True)
    )


def plain_row_locks(scan_: Scan, data: dict | None = None) -> list[str]:
    """L6(b): every explicit lock of a header, a job row or an AutoQueue row lets a foreign-key check pass -- except
    the delete lock L6(c) names (a header its transaction deletes), listed in its entry's ``delete_locks``."""
    out = []
    known = entries(data)
    for site in scan_.sites:
        if signature(site) in known.get(key(site), {}).get("delete_locks", {}):
            continue
        if site.kind == "lock-for-update":
            calls = _lock_calls(scan_.function_node(site.file, site.func), site.line)
            if not calls or not all(_no_key_update(c) for c in calls):
                out.append(f"{key(site)}:{site.line} {site.model} FOR UPDATE")
        elif site.kind == "lock-raw" and site.columns and site.columns[0] == "for-update":
            out.append(f"{key(site)}:{site.line} raw FOR UPDATE of {site.model}")
        elif site.kind in {"lock-lock_nowait", "lock-lock_nowait_where"}:
            out.append(f"{key(site)}:{site.line} lock_nowait of {site.model} (FOR UPDATE NOWAIT)")
    return out


def _is_queue_column(node) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "queue_id"
        and isinstance(node.value, ast.Name)
        and node.value.id == "PrintQueueItem"
    )


def _restricts_the_queue(condition) -> bool:
    """A conjunct of a ``where`` that confines the rows to named queues: ``PrintQueueItem.queue_id == …``,
    ``PrintQueueItem.queue_id.in_(…)``, ``tuple_(…, PrintQueueItem.queue_id, …).in_(…)``, inside ``and_(…)`` or
    either branch of a conditional expression. ``or_``, a negation, a projection or an ordering confine nothing."""
    if isinstance(condition, ast.Compare):
        return (
            _is_queue_column(condition.left)
            and len(condition.ops) == 1
            and isinstance(condition.ops[0], ast.Eq)
            and not _is_queue_column(condition.comparators[0])
        )
    if isinstance(condition, ast.IfExp):
        return _restricts_the_queue(condition.body) or _restricts_the_queue(condition.orelse)
    if not isinstance(condition, ast.Call):
        return False
    func = condition.func
    if isinstance(func, ast.Name) and func.id == "and_":
        return any(_restricts_the_queue(arg) for arg in condition.args)
    if isinstance(func, ast.Attribute) and func.attr == "in_":
        target = func.value
        if _is_queue_column(target):
            return True
        return (
            isinstance(target, ast.Call)
            and isinstance(target.func, ast.Name)
            and target.func.id == "tuple_"
            and any(_is_queue_column(arg) for arg in target.args)
        )
    return False


def _names_the_queue(statement) -> bool:
    """A ``where`` / ``filter`` of the statement confines the locked rows to named queues (review B1-08): a mention
    of ``queue_id`` in the projection, an ordering or an ineffective condition does not count."""
    for node in ast.walk(statement):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"where", "filter"}
            and any(_restricts_the_queue(arg) for arg in node.args)
        ):
            return True
    return False


def unaddressed_job_locks(scan_: Scan, data: dict | None = None) -> list[str]:
    """L1 (review B1-02): a lane holder locks a job row only in the queue whose lane it holds -- ``queue_id`` in the
    statement before ``FOR …``, never the id alone (a row that moved meanwhile would be waited for behind another
    lane's holder) and never ``db.get(…, with_for_update=…)``, which cannot say it. A lock that needs no queue (a
    ``printing`` row, which no writer moves) is listed in its entry's ``unaddressed_locks`` with the reason."""
    out = []
    known = entries(data)
    for site in scan_.sites:
        if site.kind != "lock-for-update" or "PrintQueueItem" not in (site.model or "").split("|"):
            continue
        entry = known.get(key(site), {})
        if entry.get("lock") not in {"lane", "helper"} or signature(site) in entry.get("unaddressed_locks", {}):
            continue
        fn = scan_.function_node(site.file, site.func)
        calls = _lock_calls(fn, site.line)
        statement = _statement_at(fn, site.line)
        if any(call.func.attr == "get" for call in calls) or statement is None or not _names_the_queue(statement):
            out.append(f"{key(site)}:{site.line} job row locked without its queue")
    return out


def order_lock_is_no_key_update(app: Path = APP) -> list[str]:
    """L6(b) also covers the order row ``order_resources.policy_for(lock=True)`` takes."""
    tree = ast.parse((app / "services" / "order_resources.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "policy_for")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    locks = [c for c in calls if c.func.attr == "with_for_update"]
    if not locks:
        return ["policy_for takes no row lock"]
    return [f"policy_for:{c.lineno} FOR UPDATE" for c in locks if not _no_key_update(c)]


# ---------------------------------------------------------------- A20: several router rows, locked first

AUTO_ROW_KINDS = {"core-update", "core-delete"}


def _statement_at(fn, line: int):
    """The innermost statement of ``fn`` that spans ``line``."""
    found = None
    for node in ast.walk(fn):
        if isinstance(node, ast.stmt) and node.lineno <= line <= (node.end_lineno or node.lineno):
            if found is None or node.lineno >= found.lineno:
                found = node
    return found


def _addresses_one_router_row(statement) -> bool:
    """A ``.where(...)`` of the statement names ``AutoQueueItem.id == …`` -- a one-row compare-and-set."""
    for node in ast.walk(statement):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "where"):
            continue
        for arg in node.args:
            if (
                isinstance(arg, ast.Compare)
                and isinstance(arg.left, ast.Attribute)
                and arg.left.attr == "id"
                and isinstance(arg.left.value, ast.Name)
                and arg.left.value.id == "AutoQueueItem"
                and len(arg.ops) == 1
                and isinstance(arg.ops[0], ast.Eq)
            ):
                return True
    return False


def auto_rows_without_prelock(scan_: Scan, data: dict) -> list[str]:
    """A20 / A13: no set-based UPDATE or DELETE of ``auto_queue_items`` runs before ``lock_auto_rows`` in its
    function -- the rows of one transaction are locked ascending, as one set, before the first write. A one-row
    compare-and-set (``AutoQueueItem.id == …``) is exempt; anything else is listed in its entry's ``auto_rows``
    (``{signature: reason}``) or fails."""
    out = []
    for site in scan_.sites:
        if site.kind not in AUTO_ROW_KINDS or "AutoQueueItem" not in (site.model or ""):
            continue
        fn = scan_.function_node(site.file, site.func)
        statement = _statement_at(fn, site.line)
        if statement is not None and _addresses_one_router_row(statement):
            continue
        prelocked = any(
            isinstance(node, ast.Call) and wi._call_name(node) == "lock_auto_rows" and node.lineno < site.line
            for node in ast.walk(fn)
        )
        if prelocked or signature(site) in entries(data).get(key(site), {}).get("auto_rows", {}):
            continue
        out.append(f"{key(site)}:{site.line} {signature(site)} writes router rows set-based without lock_auto_rows")
    funcs = scan_.functions()
    for name, entry in entries(data).items():
        names = {signature(site) for site in funcs.get(name, ())}
        out.extend(f"{name}: auto_rows {sig} names no site" for sig in entry.get("auto_rows", {}) if sig not in names)
    return sorted(out)


# ---------------------------------------------------------------- A25: a row enters a queue whose header held

HEADER_READS = {"queue_header_holds", "read_queue_occupancy"}


def rows_built_without_a_header_read(scan_: Scan, data: dict) -> list[str]:
    """A25 / A13: a function that builds a printer row (or the function its nested builder lives in) re-reads the
    target header under the lane -- ``queue_header_holds`` or ``read_queue_occupancy`` -- or its entry names where
    that happens (``header``)."""
    out = []
    for name, sites in sorted(scan_.functions().items()):
        if not any(s.kind == "construct" and "PrintQueueItem" in (s.model or "").split("|") for s in sites):
            continue
        entry = entries(data).get(name, {})
        if entry.get("header"):
            continue
        file, _, qualname = name.partition("::")
        info = scan_.by_rel[file]
        outer = qualname.split(".")
        scopes = [".".join(outer[:i]) for i in range(len(outer), 0, -1)]
        fns = [info.functions[q] for q in scopes if q in info.functions]
        if not any(
            isinstance(node, ast.Call) and wi._call_name(node) in HEADER_READS for fn in fns for node in ast.walk(fn)
        ):
            out.append(f"{name}: builds a printer row with no header re-read under its lane")
    return out


# ---------------------------------------------------------------- A26: the file-path lock has four takers

PATH_LOCK_TAKERS = {
    "services/archive.py::ArchiveService._attach_3mf_to_archive_locked",
    "services/archive.py::ArchiveService._delete_archive_locked",
    "services/archive.py::ArchiveService.cleanup_archive_removal",
    "services/user_content_delete.py::delete_content",
}


def path_lock_takers(app: Path = APP) -> set[str]:
    """Every module-level function or method that enters ``archive_file_reference_scope`` (A26 / A13: exactly the
    four of :data:`PATH_LOCK_TAKERS`; a fifth fails)."""
    found = set()
    for path in sorted(app.rglob("*.py")):
        rel = path.relative_to(app).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owners = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                owners.append((node.name, node))
            elif isinstance(node, ast.ClassDef):
                owners.extend(
                    (f"{node.name}.{item.name}", item)
                    for item in node.body
                    if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                )
        for name, fn in owners:
            if name == "archive_file_reference_scope":
                continue
            if any(
                isinstance(call, ast.Call) and wi._call_name(call) == "archive_file_reference_scope"
                for call in ast.walk(fn)
            ):
                found.add(f"{rel}::{name}")
    return found


# ---------------------------------------------------------------- A9 / A13: a halt has one writer

HALT_KINDS = {"error", "paused"}
#: Halts written outside ``halt_lane`` on purpose, with why.
HALT_EXEMPT = {
    "services/print_reconciliation.py::_repair_terminal_queue_items": (
        "the terminal autorepair: a structural close of claims proved stale on an inactive printer, under its lanes "
        "and the archives' guards; it always holds the queue (is_paused + paused unless error) -- 'inactive queues "
        "stay paused', a rule halt_lane's live-owner branch does not express"
    ),
}
HEADER_NAMES = {"queue", "q", "_pq", "header", "printer_queue", "pq"}


def halts_outside_halt_lane(app: Path = APP) -> list[str]:
    """A header's ``status`` set to ``error`` / ``paused`` anywhere but ``queue_counters.halt_lane`` -- a Core
    ``update(PrinterQueue)...values(status=...)`` or an attribute write on a header-named object."""
    out = []
    for path in sorted(app.rglob("*.py")):
        rel = path.relative_to(app).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if (rel == "services/queue_counters.py" and fn.name == "halt_lane") or f"{rel}::{fn.name}" in HALT_EXEMPT:
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "values":
                    halts = [
                        k
                        for k in node.keywords
                        if k.arg == "status" and isinstance(k.value, ast.Constant) and k.value.value in HALT_KINDS
                    ]
                    on_header = any(
                        isinstance(sub, ast.Call)
                        and wi._call_name(sub) == "update"
                        and sub.args
                        and isinstance(sub.args[0], ast.Name)
                        and sub.args[0].id == "PrinterQueue"
                        for sub in ast.walk(node.func.value)
                    )
                    if halts and on_header:
                        out.append(f"{rel}::{fn.name}:{node.lineno}")
                elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Attribute)
                            and target.attr == "status"
                            and isinstance(target.value, ast.Name)
                            and target.value.id in HEADER_NAMES
                            and node.value.value in HALT_KINDS
                        ):
                            out.append(f"{rel}::{fn.name}:{node.lineno}")
    return sorted(set(out))


# ---------------------------------------------------------------- A16 / A13: a Core status write voids the proof


def status_writes_keeping_the_proof(app: Path = APP) -> list[str]:
    """A Core ``update(PrintQueueItem)...values(status=...)`` also writes ``deferred_claim_token`` (NULL) -- the
    ORM validator that voids the deferral proof does not run for Core statements -- except ``defer_claim``, which
    writes the proof itself. ``values(**mapping)`` is read as the mapping's literal keys when it is a dict display."""
    out = []
    for path in sorted(app.rglob("*.py")):
        rel = path.relative_to(app).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef) or fn.name == "defer_claim":
                continue
            for node in ast.walk(fn):
                if not (
                    isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "values"
                ):
                    continue
                on_jobs = any(
                    isinstance(sub, ast.Call)
                    and wi._call_name(sub) == "update"
                    and sub.args
                    and isinstance(sub.args[0], ast.Name)
                    and sub.args[0].id == "PrintQueueItem"
                    for sub in ast.walk(node.func.value)
                )
                if not on_jobs:
                    continue
                keys = {k.arg for k in node.keywords if k.arg}
                for k in node.keywords:
                    if k.arg is None and isinstance(k.value, ast.Dict):
                        keys |= {key.value for key in k.value.keys if isinstance(key, ast.Constant)}
                unchanged = any(
                    k.arg == "status" and isinstance(k.value, ast.Attribute) and k.value.attr == "status"
                    for k in node.keywords
                )  # ``status=PrintQueueItem.status``: a row lock, no status change
                if "status" in keys and "deferred_claim_token" not in keys and not unchanged:
                    out.append(f"{rel}::{fn.name}:{node.lineno}")
    return sorted(set(out))


# ---------------------------------------------------------------- A9 / A13: a release names what it releases


def bare_releases(scan_: Scan, data: dict) -> list[str]:
    """A ``set_queue_idle(db, q)`` with neither ``expected_item_id`` nor ``stale_only`` outside an ``L2c`` entry (or
    one that names why in ``bare_release``) -- a release that does not say which claim it ends can end another's."""
    out = []
    for name in sorted(scan_.functions()):
        entry = entries(data).get(name, {})
        if entry.get("lock") == "L2c" or entry.get("bare_release"):
            continue
        file, _, qualname = name.partition("::")
        fn = scan_.by_rel[file].functions.get(qualname)
        if fn is None:
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and wi._call_name(node) == "set_queue_idle"
                and not {k.arg for k in node.keywords} & {"expected_item_id", "stale_only"}
            ):
                out.append(f"{name}:{node.lineno}")
    return sorted(set(out))
