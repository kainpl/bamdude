"""The one place a person's Stop is sent from, and marked.

A stopped print ends as the printer's ``failed`` / ``aborted``. Only the mark
(``main.mark_printer_stopped_by_user``, read once by the completion handler)
tells it from a real failure: without it the queue goes to error, "print failed"
is announced, the owner is mailed a failure and an AutoQueue job becomes a
return candidate -- for a print somebody stopped on purpose. Every door calls
here so none can send the stop and forget the mark (upstream 9c7db6e37).

The mark is set in the same synchronous step as the send, before the caller's
next await: the printer's terminal report is handled on the event loop and
cannot overtake it.
"""

import logging

from backend.app.services.printer_manager import printer_manager

logger = logging.getLogger(__name__)

# The HMS dialog's action that stops the print (``data/hms_actions.json``).
HMS_STOP_ACTION = "STOP_PRINTING"


def _mark(printer_id: int, *, unless_idle: bool = False) -> None:
    from backend.app.main import mark_printer_stopped_by_user

    mark_printer_stopped_by_user(printer_id, unless_idle=unless_idle)


def mark_stopped_by_user(printer_id: int, *, unless_idle: bool = True) -> None:
    """Record that the print on this printer is being stopped by a person.

    Never raises: the stop itself matters more than its label.
    """
    try:
        _mark(printer_id, unless_idle=unless_idle)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to mark printer %s as user-stopped: %s", printer_id, exc)


def stop_print_by_user(printer_id: int, *, mark_when_unsent: bool = False, unless_idle: bool = True) -> bool:
    """Send the stop and mark it; returns whether the command was sent.

    ``unless_idle`` (default) leaves no mark on a printer that reports no print
    loaded -- that stop stops nothing, and its mark would wait for some later
    print's failure. ``mark_when_unsent`` is the queue's Stop of a job no run
    is bound for (still being sent or heated): its row is cancelled whether or
    not the printer could be reached, so the mark stands either way. A bound
    run whose stop could not be sent is not stopped and gets no mark.
    """
    try:
        sent = bool(printer_manager.stop_print(printer_id))
    except Exception as exc:  # noqa: BLE001
        logger.error("Error sending the stop command to printer %s: %s", printer_id, exc)
        sent = False
    if sent or mark_when_unsent:
        mark_stopped_by_user(printer_id, unless_idle=unless_idle)
    return sent


def mark_hms_action(printer_id: int, action: str) -> None:
    """The HMS dialog's Stop Printing is a person's stop like any other."""
    if action == HMS_STOP_ACTION:
        mark_stopped_by_user(printer_id, unless_idle=True)
