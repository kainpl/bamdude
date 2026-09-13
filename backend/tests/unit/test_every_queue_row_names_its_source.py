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


def _sites() -> list[tuple[str, str, int, ast.Call]]:
    """Every queue-row construction under ``backend/app``, with its scope.

    The scope is the dotted chain of enclosing classes and functions, so that an
    exemption can name one writer instead of a whole module — ``manager.py``
    holds two writers and ``background_dispatch.py`` holds a dispatcher beside
    its calibration door.
    """
    found: list[tuple[str, str, int, ast.Call]] = []

    def walk(node: ast.AST, where: str, scope: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                inner = (*scope, child.name)
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id in _MODELS
                # The model's own class statement is not a construction, and
                # neither is a bare ``PrintQueueItem`` used as a type.
                and child.keywords
            ):
                found.append((where, ".".join(scope), child.lineno, child))
            walk(child, where, inner)

    for path in sorted(_BACKEND.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        walk(tree, path.relative_to(_BACKEND).as_posix(), ())
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


def test_no_writer_inserts_a_queue_row_behind_the_constructor():
    """The AST guard above only sees constructors, so nothing else may write rows.

    A ``session.execute(insert(PrintQueueItem), [...])`` would create runnable
    jobs the guard cannot read — the one shape that makes this whole test
    decorative. There is no such writer today and there must not be one.
    """
    offenders: list[str] = []
    for path in sorted(_BACKEND.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "insert"):
                continue
            if any(isinstance(arg, ast.Name) and arg.id in _MODELS for arg in node.args):
                offenders.append(f"{path.relative_to(_BACKEND).as_posix()}:{node.lineno}")
    assert not offenders, (
        f"{offenders} builds queue rows with a bulk insert(), which the construction-site guard "
        "cannot see. Write the rows through the model constructor so the capture rule is "
        "enforceable, or extend this guard to cover the statement."
    )
