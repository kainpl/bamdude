"""Joining a print already in progress must fill the object list.

⚠️ Measured on a live farm, twice: after a backend restart the Skip Objects
button was dark on 3 of 4 machines printing the *same file*, and stayed dark
until somebody opened the dialog in the web — the one action that calls the
route that loads. An operator on a phone could not reach it at all.

The cause is that ``on_print_start`` fires on a *transition* into printing, and
a process that starts mid-plate never sees one. The connect-edge sweep is the
only hook that runs in that situation, so the load belongs there.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.app.services.print_reconciliation import _load_objects_for_a_print_already_running

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _archive(**kw):
    return SimpleNamespace(id=7, file_path="job.3mf", plate_index=1, extra_data=None, **kw)


class _Session:
    """Enough async-session surface for one ``select``."""

    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, _stmt):
        scalars = MagicMock()
        scalars.first.return_value = self._row
        result = MagicMock()
        result.scalars.return_value = scalars
        return result


def _patched(row, loader):
    return (
        patch("backend.app.core.database.async_session", lambda: _Session(row)),
        patch("backend.app.services.archive.load_objects_from_archive_into_state", loader),
    )


async def test_a_running_printer_gets_its_objects_loaded():
    loader = MagicMock(return_value=True)
    session_patch, loader_patch = _patched(_archive(), loader)

    with session_patch, loader_patch:
        await _load_objects_for_a_print_already_running(3, "RUNNING")

    loader.assert_called_once()
    assert loader.call_args.kwargs["is_retrigger"] is True, (
        "an unreadable file must leave the live state alone — this IS the print already in progress"
    )


async def test_a_paused_printer_counts_too():
    """A plate can be paused when BamDude comes up, and skipping is allowed in
    PAUSE exactly as in RUNNING."""
    loader = MagicMock(return_value=True)
    session_patch, loader_patch = _patched(_archive(), loader)

    with session_patch, loader_patch:
        await _load_objects_for_a_print_already_running(3, "PAUSE")

    loader.assert_called_once()


async def test_an_idle_printer_is_not_touched():
    """No print, nothing to load — and the state must not be reset either."""
    loader = MagicMock(return_value=True)
    session_patch, loader_patch = _patched(_archive(), loader)

    with session_patch, loader_patch:
        await _load_objects_for_a_print_already_running(3, "IDLE")

    loader.assert_not_called()


async def test_no_archive_yet_is_not_an_error():
    """The 3MF may still be downloading; that path loads objects when it lands."""
    loader = MagicMock(return_value=True)
    session_patch, loader_patch = _patched(None, loader)

    with session_patch, loader_patch:
        await _load_objects_for_a_print_already_running(3, "RUNNING")

    loader.assert_not_called()


async def test_a_failure_never_reaches_the_connect_path():
    """This runs on the connect edge. A convenience load must not stop a
    printer from coming online."""
    loader = MagicMock(side_effect=RuntimeError("corrupt 3MF"))
    session_patch, loader_patch = _patched(_archive(), loader)

    with session_patch, loader_patch:
        await _load_objects_for_a_print_already_running(3, "RUNNING")
