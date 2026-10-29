"""The process's cyclic-collector policy: one boundary at start-up, one owner (spec process-gc-latency r2).

Importing the application leaves close to 0.8 M container objects tracked by the cyclic collector —
FastAPI dependants, pydantic fields, SQLAlchemy mappers, the route table — and a full collection
walks every one of them: 330–480 ms during which the event loop answers nothing, whichever thread
started the collection (measured 2026-10-28/29 against the T22 gates). Nearly all of that heap lives
as long as the process, so ``prepare_startup_heap`` collects what the imports left unreachable and
moves the rest to the permanent generation once (``gc.freeze``); later collections walk only what
the running server created. The collector stays on and its thresholds stay as they are.

⚠️ A frozen object is never examined for cyclic garbage again. Reference counting still frees it,
but an object frozen here that later drops out of reach while inside a reference cycle stays in
memory with everything it holds. So the boundary is the FIRST thing the first lifespan does —
before the farm guard, the database or any manager — and never again: freezing a server that has
started would keep its sessions, tasks and clients forever. What exists at the boundary was
inventoried when this was introduced: the only instances a running server rebinds are the
import-time engine and session factory, which their imported aliases keep reachable anyway.

The state belongs to the process, not to a loop or an app: a second lifespan, a restore or a failed
start never decides again. ``BAMDUDE_GC_STARTUP_FREEZE=off`` keeps the stock collector; a value
other than on/off refuses the start. A host that switched the collector off or froze objects of its
own, an interpreter outside the measured cells (free-threaded builds, other implementations,
versions nobody measured) and a process already working keep the collector as they have it, with
one record that names why.

⚠️ "Froze objects of its own" is measured from this module's import, which ``main`` does at import
time: CPython 3.12 starts with a frozen set of its own (377 tuples on the measured build, 0 on 3.14),
so a non-zero count at start-up is no host's policy. A host that freezes BEFORE importing the
application is indistinguishable from that and is expected to use the opt-out.
"""

import gc
import logging
import os
import sys
import sysconfig
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import SimpleNamespace

logger = logging.getLogger(__name__)

ENV = "BAMDUDE_GC_STARTUP_FREEZE"
_ON = frozenset({"", "on", "true", "1"})
_OFF = frozenset({"off", "false", "0"})

# The interpreter families whose collector was measured against the gates. A family is one
# collector design: 3.14.0–3.14.4 shipped an incremental collector reverted in 3.14.5, so those are
# a family of their own.
_MEASURED = ((3, 12),)


class GcPolicyConfigError(ValueError):
    """``BAMDUDE_GC_STARTUP_FREEZE`` holds something other than on or off."""


@dataclass(frozen=True)
class Outcome:
    state: str  # "enabled" | "skipped"
    reason: str | None
    python: str
    elapsed_ms: float
    frozen: int


def _interpreter() -> SimpleNamespace:
    return SimpleNamespace(
        implementation=sys.implementation.name,
        version=tuple(sys.version_info[:3]),
        free_threaded=bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
    )


def _interpreter_refusal(facts) -> str | None:
    if facts.implementation != "cpython":
        return "unverified_interpreter"
    if facts.free_threaded:
        return "free_threaded"
    if tuple(facts.version[:2]) not in _MEASURED:
        return "unverified_interpreter"
    return None


def _already_working() -> str | None:
    """Whether the application has already opened its database or a printer, read off the two
    singletons that would hold them; a module nobody imported has started nothing."""
    database = sys.modules.get("backend.app.core.database")
    if database is not None:
        pool = database.engine.sync_engine.pool
        if pool.checkedin() or pool.checkedout():
            return "database_open"
    printers = sys.modules.get("backend.app.services.printer_manager")
    if printers is not None and printers.printer_manager._clients:
        return "printer_clients"
    return None


def _mode(environ: Mapping[str, str]) -> bool:
    raw = environ.get(ENV)
    value = (raw or "").strip().lower()
    if value in _ON:
        return True
    if value in _OFF:
        return False
    raise GcPolicyConfigError(f"{ENV} must be 'on' or 'off', not {raw!r}")


class _Boundary:
    def __init__(
        self,
        collector=gc,
        *,
        interpreter: Callable[[], SimpleNamespace] = _interpreter,
        already_working: Callable[[], str | None] = _already_working,
    ):
        self._gc = collector
        self._interpreter = interpreter
        self._already_working = already_working
        self._lock = threading.Lock()
        self._outcome: Outcome | None = None
        # What the interpreter (or whoever ran before the application) froze; anything frozen or
        # unfrozen after this is someone else's policy.
        self._frozen_at_import = collector.get_freeze_count()

    def prepare(self, environ: Mapping[str, str]) -> Outcome:
        on = _mode(environ)
        with self._lock:
            if self._outcome is not None:
                return self._outcome
            facts = self._interpreter()
            python = ".".join(str(part) for part in facts.version)
            reason = self._refusal(on, facts)
            if reason is not None:
                self._outcome = Outcome("skipped", reason, python, 0.0, self._gc.get_freeze_count())
            else:
                start = time.perf_counter()
                self._gc.collect(2)
                self._gc.freeze()
                elapsed = (time.perf_counter() - start) * 1000
                self._outcome = Outcome("enabled", None, python, round(elapsed, 1), self._gc.get_freeze_count())
            self._report(self._outcome)
            return self._outcome

    def _refusal(self, on: bool, facts) -> str | None:
        if not on:
            return "disabled_by_env"
        if not self._gc.isenabled():
            return "gc_disabled"
        if self._gc.get_freeze_count() != self._frozen_at_import:
            return "already_frozen"
        refusal = _interpreter_refusal(facts)
        if refusal is not None:
            return refusal
        if self._already_working() is not None:
            return "late_start"
        return None

    @staticmethod
    def _report(outcome: Outcome) -> None:
        if outcome.state == "enabled":
            logger.info(
                "GC startup boundary: enabled (Python %s, prepared in %.1f ms, %d objects frozen)",
                outcome.python,
                outcome.elapsed_ms,
                outcome.frozen,
            )
            return
        level = logging.INFO if outcome.reason == "disabled_by_env" else logging.WARNING
        logger.log(
            level,
            "GC startup boundary: skipped, %s (Python %s); the stock collector stays",
            outcome.reason,
            outcome.python,
        )


_boundary = _Boundary()


def prepare_startup_heap() -> Outcome:
    """Called once, first, by the application's lifespan; every later call returns the first answer."""
    return _boundary.prepare(os.environ)
