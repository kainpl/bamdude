"""Every queue row is built with a captured source — or is a named exemption.

Spec: ``60-specs/queue-source-spool-spec.md`` S1 — "a new runnable job with a
supported source has a ready, verified queue source". The only thing that can
break S1 is a *writer*: a new door that builds a ``PrintQueueItem`` or an
``AutoQueueItem`` without going through ``services/queue_source_capture.py``
produces a job whose bytes still live on somebody's laptop, and nothing about
that row looks wrong until the share goes away mid-print.

There are eleven such doors today, spread over eight modules — the routes, the
two schedulers, the batch helper, the clone, the Telegram bot and the virtual
printer — and a twelfth is one feature away. No feature test for the new door
would ever ask this question, so the guard is over the **construction sites
themselves**, the way ``test_queue_item_attribution`` guards ``created_by_id``
and ``test_every_model_module_is_registered`` guards the model list: a directory
walk and an AST, so that nothing has to be imported and the failure names the
file and the line somebody has to fix.

**The rule.** Every ``PrintQueueItem(...)`` / ``AutoQueueItem(...)`` call under
``backend/app`` passes both ``queue_source_id=`` and ``source_snapshot=``, and
neither may be the literal ``None`` — a literal is how a writer silently opts
out. A site that genuinely has nothing to capture is named in
:data:`_EXEMPT_SITES` with the reason, which is the only place §2's exemptions
are allowed to live in code this test can see.

**Why a conditional ``None`` passes.** ``queue_batch.claim_printer_for_direct_print``
builds one row for two things: a direct print BamDude is sending (captured) and
an external print it never sent (§2's exemption, reached through
``main.mark_queue_printing_for_printer``/``_adopt_running_print``). It writes
``queue_source_id=None if source is None else source.id`` — the exemption
decided per call, at the one site that serves both. That is a deliberate
expression, not an omission, which is exactly the difference between it and a
bare ``None``.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_BACKEND = pathlib.Path(__file__).resolve().parents[2] / "app"

_MODELS = ("PrintQueueItem", "AutoQueueItem")
_REQUIRED = ("queue_source_id", "source_snapshot")

# (path relative to backend/app, dotted qualname of the enclosing scope) → why
# this row has no source to capture. Spec §2 lists the exemptions; each entry
# here must be one of them.
_EXEMPT_SITES: dict[tuple[str, str], str] = {
    ("services/background_dispatch.py", "enqueue_calibration_print"): (
        "spec §2: a service calibration job. Its source is an asset the "
        "calibration service slices into the library for this one print and "
        "owns the lifecycle of; the job is never repeated from stored bytes."
    ),
}


def _model_aliases(tree: ast.AST) -> set[str]:
    """Names a module gave one of the queue models — ``_MODEL = PrintQueueItem``.

    A helper parameterised by tier is how a writer would arrive holding the model
    as a value (a promotion or a rebalance that writes into either queue), and a
    guard that matched only the model's own spelling would not see the
    construction at all. One level of aliasing, which is what such a helper
    looks like; an alias of an alias is not worth the walk.
    """
    return {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name) and node.value.id in _MODELS
        for target in node.targets
        if isinstance(target, ast.Name)
    } | {
        node.target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.value, ast.Name)
        and node.value.id in _MODELS
        and isinstance(node.target, ast.Name)
    }


def _sites_in(source: str, where: str) -> list[tuple[str, str, int, ast.Call]]:
    """Every queue-row construction in one module's source, with its scope.

    The scope is the dotted chain of enclosing classes and functions, so that an
    exemption can name one writer instead of a whole module — ``manager.py``
    holds two writers and ``background_dispatch.py`` holds a dispatcher beside
    its calibration door.

    Takes source text rather than a path so that the guard's own blind spots can
    be tested on synthetic writers (see ``_BLIND_SPOTS`` below). A guard that
    reads *almost* everything is worse than one that reads nothing: it looks like
    coverage.
    """
    tree = ast.parse(source, filename=where)
    names = (*_MODELS, *_model_aliases(tree))
    found: list[tuple[str, str, int, ast.Call]] = []

    def walk(node: ast.AST, scope: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                inner = (*scope, child.name)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id in names:
                # ⚠️ EVERY call, ``PrintQueueItem()`` with no keywords included. An
                # earlier version of this guard also required ``child.keywords``,
                # on the theory that it was excluding the model's own class
                # statement and bare uses as a type — neither of which is an
                # ``ast.Call``, so it excluded nothing and opened the one hole a
                # writer can walk through without noticing: build the row empty,
                # assign the columns one at a time, ``session.add`` it, and the
                # guard saw a site with no keywords and skipped it.
                found.append((where, ".".join(scope), child.lineno, child))
            walk(child, inner)

    walk(tree, ())
    return found


def _sites() -> list[tuple[str, str, int, ast.Call]]:
    """Every queue-row construction under ``backend/app``."""
    found: list[tuple[str, str, int, ast.Call]] = []
    for path in sorted(_BACKEND.rglob("*.py")):
        found.extend(_sites_in(path.read_text(encoding="utf-8"), path.relative_to(_BACKEND).as_posix()))
    return found


def _literal_none(value: ast.expr) -> bool:
    return isinstance(value, ast.Constant) and value.value is None


def test_the_guard_reads_the_place_the_writers_live():
    """A guard that silently reads nothing passes forever."""
    assert _BACKEND.is_dir(), f"{_BACKEND} moved — this drift guard is reading the wrong place"
    assert len(_sites()) >= 10, "the construction sites vanished — was a model renamed?"


@pytest.mark.parametrize(
    ("where", "scope", "line", "call"),
    _sites(),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_every_queue_row_is_built_from_a_captured_source(where, scope, line, call):
    keywords = {kw.arg: kw.value for kw in call.keywords if kw.arg}
    reason = _EXEMPT_SITES.get((where, scope))

    missing = [name for name in _REQUIRED if name not in keywords]
    blanked = [name for name in _REQUIRED if name in keywords and _literal_none(keywords[name])]

    if reason is not None:
        assert missing or blanked, (
            f"{where}:{line} ({scope}) is listed in _EXEMPT_SITES but now passes a captured "
            "source. Drop the exemption — a stale one silences the guard for a writer that "
            "has since learned to capture."
        )
        return

    assert not missing, (
        f"{where}:{line} ({scope}) builds a {call.func.id} without {missing}. A queued job "
        "must print bytes BamDude has already copied into queue-spool (spec S1): capture "
        "through services/queue_source_capture.py and pass queue_source_id + source_snapshot. "
        "If this row genuinely has nothing to capture (spec section 2: an external print, a "
        "calibration job), add it to _EXEMPT_SITES with the reason."
    )
    assert not blanked, (
        f"{where}:{line} ({scope}) passes {blanked} as a literal None, which is opting out of "
        "the queue spool without saying so. If the row can have no source, decide it per call "
        "the way queue_batch does for an external print, or name this site in _EXEMPT_SITES."
    )


def test_no_exemption_names_a_site_that_is_gone():
    """A moved or renamed writer must not keep its exemption by accident."""
    live = {(where, scope) for where, scope, _line, _call in _sites()}
    stale = sorted(site for site in _EXEMPT_SITES if site not in live)
    assert not stale, (
        f"_EXEMPT_SITES names {stale}, which builds no queue row any more. Drop the entry; "
        "if the writer moved, re-read whether the exemption still holds where it went."
    )


def _bulk_inserts_in(source: str, where: str) -> list[str]:
    """``insert`` calls in one module that name a queue model, as ``where:line``.

    ⚠️ The callee is matched by **spelling**, not by identity: this codebase
    habitually imports SQLAlchemy under an alias (``sa_select``, ``sa_func``,
    ``sa_insert``) and sometimes as a module (``sa.insert``), so a check for the
    bare name ``insert`` would have let its own house style through.

    ⚠️ And the model is looked for **anywhere in the callee expression** as well as
    in the arguments, because ``PrintQueueItem.__table__.insert()`` spells the
    model on the left of the call and passes nothing at all.
    """
    tree = ast.parse(source, filename=where)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        spelled_insert = (isinstance(callee, ast.Name) and callee.id.endswith("insert")) or (
            isinstance(callee, ast.Attribute) and callee.attr == "insert"
        )
        if not spelled_insert:
            continue
        names_a_model = any(isinstance(arg, ast.Name) and arg.id in _MODELS for arg in node.args) or any(
            isinstance(inner, ast.Name) and inner.id in _MODELS for inner in ast.walk(callee)
        )
        if names_a_model:
            offenders.append(f"{where}:{node.lineno}")
    return offenders


def test_no_writer_inserts_a_queue_row_behind_the_constructor():
    """The AST guard above only sees constructors, so nothing else may write rows.

    A ``session.execute(insert(PrintQueueItem), [...])`` would create runnable
    jobs the guard cannot read — the one shape that makes this whole test
    decorative. There is no such writer today and there must not be one.
    """
    offenders: list[str] = []
    for path in sorted(_BACKEND.rglob("*.py")):
        offenders.extend(_bulk_inserts_in(path.read_text(encoding="utf-8"), path.relative_to(_BACKEND).as_posix()))
    assert not offenders, (
        f"{offenders} builds queue rows with a bulk insert(), which the construction-site guard "
        "cannot see. Write the rows through the model constructor so the capture rule is "
        "enforceable, or extend this guard to cover the statement."
    )


# --------------------------------------------------------------------------- #
# The guard's own blind spots
# --------------------------------------------------------------------------- #
#
# Every shape below was, at some point, a writer that this guard could not see —
# three of them found by a reviewer rather than by the guard, which is the whole
# argument for pinning them here rather than in a report: a future refactor of
# the walk above cannot quietly reopen one.

_BLIND_SPOTS: dict[str, str] = {
    "a row built with no keywords, its columns assigned afterwards": """
item = PrintQueueItem()
item.queue_id = 1
item.status = "pending"
session.add(item)
""",
    "a model reached through a module-level alias": """
_MODEL = AutoQueueItem


def enqueue(session):
    session.add(_MODEL(status="pending", position=1))
""",
    "a model aliased with an annotation": """
_MODEL: type = PrintQueueItem


def enqueue(session):
    session.add(_MODEL(queue_id=1, status="pending"))
""",
    "the splat that hides which columns are set": """
def enqueue(session, fields):
    session.add(PrintQueueItem(**fields))
""",
}


@pytest.mark.parametrize("shape", sorted(_BLIND_SPOTS), ids=lambda name: name)
def test_the_guard_sees_a_writer_shaped_like_this(shape: str):
    """Seen AND reported: a site the walk finds but the rule waves through is no
    better than one it never found."""
    sites = _sites_in(_BLIND_SPOTS[shape], "<probe>")
    assert sites, f"the guard does not even see {shape!r} — widen _sites_in"

    for _where, _scope, _line, call in sites:
        names = {kw.arg for kw in call.keywords if kw.arg}
        assert not set(_REQUIRED) <= names, f"{shape!r} passed a captured source — fix the probe, not the guard"


def test_a_compliant_site_is_accepted():
    """The other half: the rule must not simply fail everything it sees."""
    source = """
def enqueue(session, source, snapshot):
    session.add(PrintQueueItem(queue_source_id=source.id, source_snapshot=snapshot, queue_id=1, status="pending"))
"""
    (site,) = _sites_in(source, "<probe>")
    names = {kw.arg: kw.value for kw in site[3].keywords if kw.arg}
    assert set(_REQUIRED) <= set(names)
    assert not any(_literal_none(names[name]) for name in _REQUIRED)


def test_a_row_inserted_through_the_models_own_table_is_seen():
    """``PrintQueueItem.__table__.insert()`` names the model on the LEFT of the
    call and passes no arguments at all — the args-only check missed it."""
    assert _bulk_inserts_in("session.execute(PrintQueueItem.__table__.insert(), rows)\n", "<probe>") == ["<probe>:1"]
    assert _bulk_inserts_in("await session.execute(sa_insert(AutoQueueItem), rows)\n", "<probe>") == ["<probe>:1"]
    assert _bulk_inserts_in("await session.execute(sa.insert(PrintQueueItem), rows)\n", "<probe>") == ["<probe>:1"]
    # Somebody else's table is not this guard's business.
    assert _bulk_inserts_in("await session.execute(sa_insert(LibraryFile), rows)\n", "<probe>") == []
