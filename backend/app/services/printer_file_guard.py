"""May a file on the printer be deleted right now? One answer for every door (G10 B).

The printer reads the running job off its own storage, so deleting that file —
or the working copy it made of it in ``/cache`` — ends the print. Every door
that deletes on the printer asks here: the file browser's delete, the card
clear, "delete the oldest recording", the tidy-up after a timelapse attach, the
cleanup after a print and the dispatcher's own deletions.

⚠️ **Permission comes only from a fresh observation.** A printer that is
disconnected, silent or has not reported yet is not "idle": its last cached
state proves nothing about what it prints now, so the answer is a refusal.
``PrinterManager.is_print_active`` answers False for exactly those cases and is
therefore no gate for a deletion.

⚠️ **A matching hash is no exception.** The next print of the same 3MF has the
same name and the same bytes; content proves a copy is ours, never that nobody
is printing from it.

The names are compared on both storages. The state does not say which medium
the job is read from, and refusing a same-named file on the other one costs a
deletion the operator can repeat after the print; guessing wrong costs a print.

What this cannot do: a ``DELE`` the firmware has already accepted is not
recalled, and a print started in the moment between the last check and that
command is not seen. The check runs as late as the transport allows — after the
wait for the printer's file channel, immediately before the command.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from backend.app.services.printer_manager import printer_manager

logger = logging.getLogger(__name__)

#: States in which the printer has a job loaded or is loading one.
ACTIVE_STATES = frozenset(printer_manager.ACTIVE_PRINT_STATES)
#: States that positively say no job is loaded. Anything else — ``unknown``, a
#: state this code has never seen — is not a permission.
SAFE_STATES = frozenset({"IDLE", "FINISH", "FAILED"})

# Refusal codes; the API answers them as ``{"error": <code>}``.
STATE_UNKNOWN = "printer_state_unknown"
ACTIVE_PRINT = "active_print"
EXPECTED_PRINT = "expected_print"

_SUFFIXES = (".gcode.3mf", ".3mf", ".gcode")
_CACHE = "/cache/"


def _stem(name: str) -> str:
    """The job name a file name stands for: last segment, lower case, print suffix dropped."""
    stem = name.replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    for suffix in _SUFFIXES:
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def _stems(*names: str | None) -> set[str]:
    """Every stem the names stand for, with the space/underscore form the firmware writes in ``/cache``."""
    found: set[str] = set()
    for name in names:
        stem = _stem(name or "")
        if stem:
            found.add(stem)
            found.add(stem.replace(" ", "_"))
    return found


def observe(printer_id: int):
    """The printer's state when it is a fresh, connected observation, else ``None``."""
    state, received_at, stale = printer_manager.peek_status(printer_id)
    if state is None or not getattr(state, "connected", False) or not received_at or stale:
        return None
    return state


def _expected_archive(printer_id: int, path: str) -> int | None:
    """The archive of the dispatch that registered this file as its next print, if any."""
    from backend.app.main import expected_print_archive  # noqa: PLC0415 — main owns the registry

    return expected_print_archive(printer_id, path)


def _printer_expects_a_print(printer_id: int) -> bool:
    from backend.app.main import printer_has_expected_print  # noqa: PLC0415 — main owns the registry

    return printer_has_expected_print(printer_id)


def _is_internal_gcode_path(gcode_file: str | None) -> bool:
    """Whether the reported G-code name is the plate member inside the 3MF rather than the 3MF itself."""
    lowered = (gcode_file or "").replace("\\", "/").lower()
    last = lowered.rsplit("/", 1)[-1]
    return "metadata/" in lowered or (last.startswith("plate_") and last.endswith(".gcode"))


def _is_active_file(state, path: str) -> bool:
    """Whether ``path`` is, or may be, a file the loaded job reads.

    With neither name reported the job cannot be told from any file, so every
    print file counts. In ``/cache`` the printer keeps the working G-code under a
    name of its own and a ``.bbl`` per job: while a job is loaded none of those
    can be proven to be another job's.
    """
    lowered = path.replace("\\", "/").lower()
    name = lowered.rsplit("/", 1)[-1]
    gcode_file = getattr(state, "gcode_file", None)
    if _is_internal_gcode_path(gcode_file):
        # ``Metadata/plate_1.gcode`` is a member of the job's archive, not a file
        # on the storage: it names nothing that could be compared.
        gcode_file = None
    active = _stems(gcode_file, getattr(state, "subtask_name", None))
    if not active:
        return name.endswith((*_SUFFIXES, ".bbl"))
    if _stem(name) in active:
        return True
    if _CACHE in lowered or lowered.startswith("cache/"):
        if name.endswith(".gcode"):
            return True
        if name.endswith(".bbl"):
            return any(f"_{stem}" in name for stem in active)
    return False


def file_refusal(
    printer_id: int, path: str, *, own_archive_id: int | None = None, needs_fresh_state: bool = True
) -> str | None:
    """Why this one file must not be deleted now, or ``None`` when it may be.

    ``own_archive_id`` is the dispatcher's own execution archive: a file it
    registered as its next print is its own to clear, another dispatch's is not.

    ``needs_fresh_state=False`` is the dispatcher's question. It proves the
    connection itself before it starts a print, so a printer that is silent for
    a moment does not end its upload; but it still never clears a file the last
    state it heard named as the loaded job's — a stale "printing Cube" is not
    proof that Cube is free.
    """
    state = observe(printer_id)
    if state is None:
        if needs_fresh_state:
            return STATE_UNKNOWN
        state = printer_manager.peek_status(printer_id)[0]
    status = getattr(state, "state", None)
    if status in ACTIVE_STATES:
        if _is_active_file(state, path):
            return ACTIVE_PRINT
    elif status not in SAFE_STATES and needs_fresh_state:
        return STATE_UNKNOWN
    expected = _expected_archive(printer_id, path)
    if expected is not None and expected != own_archive_id:
        return EXPECTED_PRINT
    return None


def wide_refusal(printer_id: int) -> str | None:
    """Why nothing may be deleted in bulk now — a card clear, or a file the printer picks itself."""
    state = observe(printer_id)
    if state is None:
        return STATE_UNKNOWN
    status = getattr(state, "state", None)
    if status in ACTIVE_STATES:
        return ACTIVE_PRINT
    if status not in SAFE_STATES:
        return STATE_UNKNOWN
    if _printer_expects_a_print(printer_id):
        return EXPECTED_PRINT
    return None


def may_delete(
    printer_id: int,
    path: str,
    *,
    own_archive_id: int | None = None,
    also: Callable[[], bool] | None = None,
    needs_fresh_state: bool = True,
) -> Callable[[], bool]:
    """A ``may_run`` for one file: asked again after the lane wait and before the command."""

    def check() -> bool:
        if also is not None and not also():
            return False
        refusal = file_refusal(printer_id, path, own_archive_id=own_archive_id, needs_fresh_state=needs_fresh_state)
        if refusal is not None:
            logger.info("[FILES] printer %s: %s is not deleted (%s)", printer_id, path, refusal)
            return False
        return True

    return check


def may_delete_wide(printer_id: int, *, also: Callable[[], bool] | None = None) -> Callable[[], bool]:
    """A ``may_run`` for a bulk deletion: asked before every step of the walk."""

    def check() -> bool:
        if also is not None and not also():
            return False
        return wide_refusal(printer_id) is None

    return check
