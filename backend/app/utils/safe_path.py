"""Containment-checked path joining.

Single source of truth for joining a user-controlled string under a trusted
parent directory. The arbitrary-file-write class reported against upstream
Bambuddy's ``routes/projects.py::import_project_file`` traced to plain
``Path / user_string`` arithmetic with no resolve + containment check —
an attacker passed an absolute path and ``Path("/lib") / "/etc"`` collapsed
to ``Path("/etc")``, so the next ``write_bytes`` landed wherever the attacker
chose. This module is the answer.

Every site that joins a path component coming from a request body, a ZIP
``namelist()``, an ``UploadFile.filename``, a printer's FTP directory listing,
or any other attacker-controlled source MUST route through ``safe_join_under``.
Sites that join trusted constants (settings paths, hardcoded subdirs) are not
in scope — those may carry a ``# SEC-PATH-OK: <reason>`` marker to document why.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException


class PathTraversalError(ValueError):
    """Raised when a join attempt would escape the trusted parent.

    Callers in API-route context let ``safe_join_under(http=True)`` raise
    ``HTTPException`` directly. Non-route callers pass ``http=False`` and catch
    ``PathTraversalError`` to decide their own response shape.
    """


def safe_join_under(parent: Path, *parts: str, http: bool = True) -> Path:
    """Join *parts* under *parent* and assert the result stays under it.

    Rejects:
    - empty / non-str parts;
    - parts containing NUL (``\\x00``);
    - parts starting with ``/`` or ``\\`` (absolute paths;
      ``Path("/lib") / "/etc"`` discards ``/lib``);
    - any sequence whose resolved form is not a descendant of *parent*'s
      resolved form (defeats ``..`` traversal even when the literal join
      doesn't look suspicious).

    Returns the resolved absolute path on success.

    When ``http=True`` (default; suitable for FastAPI routes), failures raise
    ``HTTPException(400, "Invalid path in upload")``. Set ``http=False`` to
    raise ``PathTraversalError`` instead — for non-route callers (e.g. a
    background task) that need finer control over the response.
    """
    if not parts:
        _fail("safe_join_under called with no parts", http)

    for part in parts:
        if not isinstance(part, str):
            _fail(f"Path part has type {type(part).__name__}, expected str", http)
        if not part:
            _fail("Empty path part", http)
        if "\x00" in part:
            _fail("NUL byte in path part", http)
        # Reject literal absolute markers up-front: pathlib collapses
        # ``Path("/a") / "/b"`` to ``Path("/b")`` so the catch-after-resolve
        # below would also fire, but rejecting here gives a clearer error and
        # avoids touching the filesystem.
        if part.startswith("/") or part.startswith("\\"):
            _fail("Absolute path part not allowed", http)

    parent_resolved = parent.resolve()
    candidate = parent
    for part in parts:
        candidate = candidate / part
    candidate_resolved = candidate.resolve()

    if not _is_relative_to(candidate_resolved, parent_resolved):
        _fail("Path escapes the parent directory", http)

    return candidate_resolved


def assert_under(parent: Path, candidate: Path, *, http: bool = True) -> Path:
    """Assert that an already-joined *candidate* path is under *parent*.

    Use when you have an existing ``Path`` (built by another helper) and need a
    containment check before writing or deleting. Equivalent to
    ``safe_join_under`` minus the per-part input validation.
    """
    parent_resolved = parent.resolve()
    candidate_resolved = candidate.resolve()
    if not _is_relative_to(candidate_resolved, parent_resolved):
        _fail("Path escapes the parent directory", http)
    return candidate_resolved


def _is_relative_to(child: Path, parent: Path) -> bool:
    # Thin wrapper kept for call-site readability. It used to carry a
    # ``relative_to`` fallback for interpreters without ``Path.is_relative_to``
    # (pre-3.9); the floor is 3.12 now, so that branch was unreachable.
    return child.is_relative_to(parent)


def _fail(reason: str, http: bool) -> None:
    if http:
        raise HTTPException(status_code=400, detail="Invalid path in upload")
    raise PathTraversalError(reason)
