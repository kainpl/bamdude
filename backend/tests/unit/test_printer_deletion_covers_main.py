"""Every module-level container of main.py leaves with a deleted printer, or says why it stays.

SQLite gives a deleted printer's id to the next printer created, so whatever
main.py keeps under that id would greet a stranger. The registry of what leaves
(``printer_deletion._ID_KEYED``) is kept by hand; this fails when a new dict or
set appears in main.py and is in neither place.
"""

import ast
from pathlib import Path

from backend.app import main
from backend.app.services import printer_deletion

# Not taken out with a printer, each for a reason. A printer-keyed container never belongs here.
NOT_TAKEN = {
    "_HMS_FAILURE_REASONS": "constant",
    "_SENSOR_TARGET_KEYS": "constant",
    "PUBLIC_API_ROUTES": "constant",
    "SETUP_WHITELIST_ROUTES": "constant",
    "_print_ams_mappings": "keyed by ARCHIVE id; taking 'printer N' out of it dropped archive N's mapping",
    "_print_ams_mapping_identities": "keyed by archive id, beside _print_ams_mappings",
    "_stage22_finish_in_flight": "keyed (printer, effect generation); the deletion advances the generation",
    "_inprint_bank_in_flight": "keyed (printer, effect generation); the deletion advances the generation",
    "_bed_cooldown_tasks": "stopped and awaited by the delete route itself (stop_bed_cooldown_tasks)",
    "_running_print_adoptions": "waiters of a restart recovery in flight; set and released by that recovery alone",
}


def _containers() -> set[str]:
    """Names of main.py's module-level dicts and sets."""
    names: set[str] = set()
    for node in ast.parse(Path(main.__file__).read_text(encoding="utf-8")).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if ast.unparse(node.annotation).startswith(("dict", "set", "defaultdict", "frozenset")):
                names.add(node.target.id)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = node.value
            literal = isinstance(value, (ast.Dict, ast.Set, ast.DictComp, ast.SetComp))
            call = isinstance(value, ast.Call) and getattr(value.func, "id", "") in (
                "dict",
                "set",
                "defaultdict",
                "frozenset",
            )
            if literal or call:
                names.add(node.targets[0].id)
    return names


def _taken() -> set[str]:
    return {name for module, name in printer_deletion._ID_KEYED if module == "backend.app.main"}


def test_every_container_of_main_is_taken_or_says_why_not():
    unaccounted = _containers() - _taken() - set(NOT_TAKEN)
    assert unaccounted == set(), (
        f"{sorted(unaccounted)}: add to printer_deletion._ID_KEYED when keyed by a printer, "
        "else to NOT_TAKEN here with the reason"
    )


def test_nothing_is_both_taken_and_exempt():
    assert _taken() & set(NOT_TAKEN) == set()


def test_the_lists_name_only_what_exists():
    existing = _containers()
    assert _taken() - existing == set()
    assert set(NOT_TAKEN) - existing == set()
