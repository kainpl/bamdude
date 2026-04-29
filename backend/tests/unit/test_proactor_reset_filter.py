"""Unit tests for the Windows Proactor cleanup-RST filter (audit A.42).

The filter must:

* Match only the *cleanup-path* `ConnectionResetError` from
  `_ProactorBasePipeTransport._call_connection_lost`, identified by
  three signals together — the message phrase, the exception subtype,
  and the platform.
* Delegate every other event-loop exception to asyncio's default
  handler so unrelated bugs stay visible.
* Be a no-op installer on non-Windows platforms.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

from backend.app.core.asyncio_handlers import (
    _is_proactor_connection_reset,
    _proactor_reset_filter,
    install_proactor_reset_filter,
)

# ---------- _is_proactor_connection_reset ----------


def test_matches_only_when_all_three_signals_align():
    with patch.object(sys, "platform", "win32"):
        ctx = {
            "exception": ConnectionResetError("WinError 10054"),
            "message": "Fatal error on transport ... _call_connection_lost",
        }
        assert _is_proactor_connection_reset(ctx) is True


def test_skips_on_non_windows_platform():
    with patch.object(sys, "platform", "linux"):
        ctx = {
            "exception": ConnectionResetError("WinError 10054"),
            "message": "Fatal error on transport ... _call_connection_lost",
        }
        assert _is_proactor_connection_reset(ctx) is False


def test_skips_when_exception_is_not_connection_reset():
    """Real `OSError`/`RuntimeError` from elsewhere in the stack must
    fall through to the default handler — otherwise we'd silence them."""
    with patch.object(sys, "platform", "win32"):
        ctx = {
            "exception": OSError("disk full"),
            "message": "_call_connection_lost",
        }
        assert _is_proactor_connection_reset(ctx) is False


def test_skips_when_message_lacks_call_connection_lost():
    """A `ConnectionResetError` raised from application code (not the
    Proactor cleanup callback) must surface — only the cleanup-path
    spam is what we suppress."""
    with patch.object(sys, "platform", "win32"):
        ctx = {
            "exception": ConnectionResetError("peer closed during request"),
            "message": "unhandled exception in coro",
        }
        assert _is_proactor_connection_reset(ctx) is False


def test_skips_when_exception_missing():
    with patch.object(sys, "platform", "win32"):
        assert _is_proactor_connection_reset({"message": "_call_connection_lost"}) is False


def test_skips_when_message_missing():
    with patch.object(sys, "platform", "win32"):
        ctx = {"exception": ConnectionResetError()}
        assert _is_proactor_connection_reset(ctx) is False


# ---------- _proactor_reset_filter ----------


def test_filter_swallows_proactor_noise_without_calling_default_handler():
    loop = MagicMock()
    with patch.object(sys, "platform", "win32"):
        ctx = {
            "exception": ConnectionResetError("WinError 10054"),
            "message": "_call_connection_lost",
        }
        _proactor_reset_filter(loop, ctx)
    loop.default_exception_handler.assert_not_called()


def test_filter_delegates_unrelated_exceptions_to_default_handler():
    loop = MagicMock()
    with patch.object(sys, "platform", "win32"):
        ctx = {
            "exception": RuntimeError("unrelated bug"),
            "message": "task exception was never retrieved",
        }
        _proactor_reset_filter(loop, ctx)
    loop.default_exception_handler.assert_called_once_with(ctx)


def test_filter_delegates_application_connection_reset_to_default_handler():
    """A `ConnectionResetError` whose message lacks the cleanup-path
    phrase looks identical to a real bug — let asyncio's default
    handler print the traceback so we can debug it."""
    loop = MagicMock()
    with patch.object(sys, "platform", "win32"):
        ctx = {
            "exception": ConnectionResetError("peer closed mid-request"),
            "message": "task exception was never retrieved",
        }
        _proactor_reset_filter(loop, ctx)
    loop.default_exception_handler.assert_called_once_with(ctx)


# ---------- install_proactor_reset_filter ----------


def test_install_returns_false_on_non_windows():
    with patch.object(sys, "platform", "linux"):
        assert install_proactor_reset_filter() is False


def test_install_returns_true_on_windows_and_sets_handler():
    loop = MagicMock()
    with patch.object(sys, "platform", "win32"):
        result = install_proactor_reset_filter(loop)
    assert result is True
    loop.set_exception_handler.assert_called_once_with(_proactor_reset_filter)


def test_install_uses_running_loop_when_loop_arg_omitted():
    fake_loop = MagicMock()
    with (
        patch.object(sys, "platform", "win32"),
        patch("backend.app.core.asyncio_handlers.asyncio.get_running_loop", return_value=fake_loop),
    ):
        install_proactor_reset_filter()
    fake_loop.set_exception_handler.assert_called_once_with(_proactor_reset_filter)
