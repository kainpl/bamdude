"""The print lifecycle's process-global state never outlives a test (``tests/process_state.py``).

``printer_manager`` is a singleton, and ``main`` and several services keep module-level containers keyed by printer.
Every test gets a fresh database, so printer ids repeat: printer 1 of the next test inherits whatever printer 1 of the
previous one left. An admission left that way made operator recovery refuse every later repair (CI, 2026-10-15); an
in-memory plate gate made the scheduler skip the next test's printer (2026-10-26). The first pair below is that
order, pinned -- it fails if the autouse fixture stops cleaning; the rest hold the list itself to account.
"""

import importlib
import re
from pathlib import Path

from backend.tests.process_state import (
    LAZY,
    PRINTER_KEYED_REGISTRIES,
    _resolve,
    clean_print_lifecycle_state,
)

APP = Path(__file__).resolve().parents[2] / "app"

#: ``printer_manager`` attributes that are not per-printer test state, with the reason.
PROCESS_SCOPED = {
    "_layer_projection_callback": "a process-lifetime callback installed by main, not a per-printer container",
    "_clients": "the MQTT client per printer; tests install one with monkeypatch.setitem",
    "_models": "model cache filled alongside a client",
    "_printer_info": "name/serial cache filled alongside a client",
    "_connected_at": "connection time, filled alongside a client",
    "_client_generations": "connection generation counter, monotonic for the process",
    "_last_trays": "last tray snapshot of a connected client",
    "_print_effect_sequence": "a counter, not a container",
    "_print_start_resolution_sequence": "a counter, not a container",
    "_print_run_binding_sequence": "a counter, not a container",
    "_print_file_analysis_generation": "a counter, not a container",
    "_print_file_analysis_diagnostics": "a bounded diagnostics object, not keyed by printer",
    "_print_lifecycle_stopping": "a flag, reset by clean_print_lifecycle_state on its own",
}


def _fill_everything():
    from backend.app.services import background_dispatch, interrupted_dispatch, print_reconciliation
    from backend.app.services.printer_manager import printer_manager

    for module_name, path in PRINTER_KEYED_REGISTRIES:
        importlib.import_module(module_name)
        owner_path, _, name = path.rpartition(".")
        owner = _resolve(module_name, owner_path) if owner_path else importlib.import_module(module_name)
        if getattr(owner, name, None) is None:  # a lazy one: create it as its owner would
            setattr(owner, name, {})
        registry = getattr(owner, name)
        if isinstance(registry, set):
            registry.add(1)
        else:
            registry[1] = object()
    printer_manager._print_lifecycle_stopping = True
    for container in (
        print_reconciliation._live_completions,
        background_dispatch._unobserved_attempts,
        interrupted_dispatch._waiting,
    ):
        if isinstance(container, set):
            container.add(1)
        else:
            container[1] = object()


def _whatever_is_left():
    from backend.app.services import background_dispatch, interrupted_dispatch, print_reconciliation
    from backend.app.services.printer_manager import printer_manager

    left = [path for module_name, path in PRINTER_KEYED_REGISTRIES if _resolve(module_name, path)]
    if getattr(printer_manager, "_print_lifecycle_stopping", False):
        left.append("printer_manager._print_lifecycle_stopping")
    for name, container in (
        ("print_reconciliation._live_completions", print_reconciliation._live_completions),
        ("background_dispatch._unobserved_attempts", background_dispatch._unobserved_attempts),
        ("interrupted_dispatch._waiting", interrupted_dispatch._waiting),
    ):
        if container:
            left.append(name)
    return left


def test_a_test_may_leave_the_print_lifecycle_dirty():
    _fill_everything()


def test_the_next_test_starts_with_a_clean_print_lifecycle():
    """Run after the one above (file order): the autouse fixture emptied everything in between."""
    assert _whatever_is_left() == []


def test_the_cleaner_empties_every_listed_container():
    _fill_everything()
    clean_print_lifecycle_state()
    assert _whatever_is_left() == []


def test_every_listed_entry_still_names_a_real_container():
    """A renamed attribute must fail here, not be skipped silently by the cleaner."""
    for module_name, path in PRINTER_KEYED_REGISTRIES:
        importlib.import_module(module_name)
        try:
            container = _resolve(module_name, path)
        except AttributeError as missing:
            raise AssertionError(str(missing)) from None
        assert container is None and path in LAZY or isinstance(container, (dict, set)), (module_name, path)


def _listed(module_name):
    return {path for name, path in PRINTER_KEYED_REGISTRIES if name == module_name}


def test_every_printer_manager_container_is_decided():
    """A new container on ``printer_manager`` -- made in ``__init__`` or lazily by another module -- must be either
    cleaned per test or process-scoped with a reason. Lazy ones are read from the source, so the result never depends
    on which test created them first."""
    from backend.app.services.printer_manager import printer_manager

    cleaned = {path.split(".", 1)[1] for path in _listed("backend.app.services.printer_manager")}
    present = {name for name, value in vars(printer_manager).items() if isinstance(value, (dict, set))}
    lazily_made = {
        match
        for path in APP.rglob("*.py")
        if path.name != "printer_manager.py"
        for match in re.findall(r"\b(?:manager|printer_manager)\.(_[a-z_]+) = ", path.read_text(encoding="utf-8"))
    }
    undecided = sorted((present | lazily_made) - cleaned - set(PROCESS_SCOPED))
    assert undecided == [], undecided


def test_every_piece_of_main_module_state_is_decided():
    """Module-level runtime state of ``main`` (lower-case names; UPPER-case ones are constant tables)."""
    main = importlib.import_module("backend.app.main")

    state = {
        name
        for name, value in vars(main).items()
        if re.fullmatch(r"_[a-z][a-z0-9_]*", name) and isinstance(value, (dict, set))
    }
    undecided = sorted(state - _listed("backend.app.main"))
    assert undecided == [], undecided
