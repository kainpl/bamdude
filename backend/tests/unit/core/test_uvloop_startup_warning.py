"""Say so at startup when the process runs on uvloop (upstream 0dfcff59).

Every launch path BamDude ships pins ``--loop asyncio`` — uvloop's TLS layer can
silently truncate a Virtual Printer FTP upload (#1896). A unit written by a
third-party script, or a native install created before install.sh gained the
flag, still runs on uvloop, and nothing told its owner. One WARNING does now.

The question is asked of the RUNNING loop, by module name: uvicorn[standard]
installs uvloop everywhere, so "does uvloop import" says nothing, and importing
it just to ask would be an ImportError on a host without it.
"""

from __future__ import annotations

import asyncio
import logging

from backend.app.core.asyncio_handlers import running_on_uvloop, warn_if_running_on_uvloop


class _UvloopLike:
    pass


_UvloopLike.__module__ = "uvloop.loop"


def test_a_uvloop_loop_is_recognised_by_its_module():
    assert running_on_uvloop(_UvloopLike()) is True


def test_asyncios_own_loop_is_not_uvloop():
    loop = asyncio.new_event_loop()
    try:
        assert running_on_uvloop(loop) is False
    finally:
        loop.close()


def test_no_running_loop_is_not_uvloop():
    assert running_on_uvloop() is False


def test_the_warning_names_the_flag(caplog):
    with caplog.at_level(logging.WARNING, logger="backend.app.core.asyncio_handlers"):
        assert warn_if_running_on_uvloop(_UvloopLike()) is True
    assert "--loop asyncio" in caplog.text


def test_asyncio_stays_quiet(caplog):
    loop = asyncio.new_event_loop()
    try:
        with caplog.at_level(logging.WARNING, logger="backend.app.core.asyncio_handlers"):
            assert warn_if_running_on_uvloop(loop) is False
    finally:
        loop.close()
    assert "uvloop" not in caplog.text
