"""Every stop a person asks for goes through one function, which also marks it.

The mark turns the printer's ``failed`` into ``cancelled``. A door that sends the
stop without it gets a failed queue, a "print failed" notification and a letter
to the owner for a print somebody stopped on purpose.
"""

import ast
from pathlib import Path

import pytest

from backend.app.services import print_control

APP = Path(__file__).resolve().parents[2] / "app"


@pytest.fixture
def calls(monkeypatch):
    seen = {"stop": [], "mark": [], "sent": True}

    def stop(printer_id):
        seen["stop"].append(printer_id)
        return seen["sent"]

    def mark(printer_id, *, unless_idle=False):
        seen["mark"].append((printer_id, unless_idle))

    monkeypatch.setattr(print_control.printer_manager, "stop_print", stop)
    monkeypatch.setattr(print_control, "_mark", mark)
    return seen


def test_a_sent_stop_is_marked(calls):
    assert print_control.stop_print_by_user(4) is True

    assert calls["stop"] == [4]
    assert calls["mark"] == [(4, True)]


def test_a_stop_that_was_not_sent_leaves_no_mark(calls):
    calls["sent"] = False

    assert print_control.stop_print_by_user(4) is False

    assert calls["mark"] == []


def test_the_queue_stop_marks_even_when_nothing_was_sent(calls):
    # The row is cancelled either way; a printer that is off has stopped already.
    calls["sent"] = False

    assert print_control.stop_print_by_user(4, mark_when_unsent=True, unless_idle=False) is False

    assert calls["mark"] == [(4, False)]


def test_a_failing_mark_does_not_fail_the_stop(calls, monkeypatch):
    def broken(printer_id, *, unless_idle=False):
        raise RuntimeError("no mark")

    monkeypatch.setattr(print_control, "_mark", broken)

    assert print_control.stop_print_by_user(4) is True


@pytest.mark.parametrize(("action", "marked"), [("STOP_PRINTING", True), ("RESUME_PRINTING", False), ("IGNORE", False)])
def test_only_the_hms_dialogs_stop_is_marked(calls, action, marked):
    print_control.mark_hms_action(4, action)

    assert calls["mark"] == ([(4, True)] if marked else [])


def test_no_door_sends_a_stop_of_its_own():
    allowed = {
        APP / "services" / "print_control.py",
        APP / "services" / "printer_manager.py",  # the manager's own pass-through to the client
    }
    offenders = []
    for path in APP.rglob("*.py"):
        if path in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "stop_print":
                offenders.append(f"{path.relative_to(APP)}:{node.lineno}")
    assert offenders == []
