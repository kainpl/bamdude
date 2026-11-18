"""``queue/job_added`` for every job that lands in a printer's queue, whichever door added it.

The event used to be published by two doors -- the REST queue route and the slicer connector -- and
every other one was silent: AutoQueue handing a job to a printer, a Virtual Printer, Retry / Clone /
Repeat, the Telegram scenes. A call per door is how the next door forgets it, so the event is tied
to the fact itself: a ``PrintQueueItem`` inserted as ``pending`` and COMMITTED.

A row inserted as anything else is not work somebody queued -- a direct or external print takes its
row already ``printing`` to hold the printer -- and is not announced. Ids are noted on the session at
flush; a savepoint that rolls back takes its own notes with it and only the root commit sends them
(the shape ``library_file_photos`` uses for its after-commit work). The publication runs as a task
after the commit and can never fail or delay it; with no relay connected nothing is scheduled.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import event
from sqlalchemy.orm import Session

from backend.app.models.print_queue import PrintQueueItem

logger = logging.getLogger(__name__)

_STATE = "queue_job_relay_added"
_tasks: set[asyncio.Task] = set()


class _Pending:
    def __init__(self) -> None:
        self.ids: list[int] = []
        self.marks: dict = {}  # savepoint -> how many ids were noted when it opened


@event.listens_for(Session, "after_flush")
def _note_added(session, flush_context) -> None:
    added = [
        obj.id
        for obj in session.new
        if isinstance(obj, PrintQueueItem) and obj.status == "pending" and obj.id is not None
    ]
    if not added:
        return
    state = session.info.get(_STATE)
    if state is None:
        state = session.info[_STATE] = _Pending()
    state.ids.extend(i for i in added if i not in state.ids)


@event.listens_for(Session, "after_transaction_create")
def _savepoint_opened(session, transaction) -> None:
    state = session.info.get(_STATE)
    if transaction.nested and state is not None:
        state.marks[transaction] = len(state.ids)


@event.listens_for(Session, "after_soft_rollback")
def _savepoint_rolled_back(session, previous_transaction) -> None:
    state = session.info.get(_STATE)
    if not previous_transaction.nested or state is None:
        return
    # A savepoint opened before the first note has no mark: everything noted since is its own.
    del state.ids[state.marks.pop(previous_transaction, 0) :]


@event.listens_for(Session, "after_transaction_end")
def _ended(session, transaction) -> None:
    if transaction.parent is None:
        session.info.pop(_STATE, None)  # a root rollback, or a close without a commit


@event.listens_for(Session, "after_commit")
def _committed(session) -> None:
    if session.in_nested_transaction():
        return  # a savepoint released: its notes stay with the transaction around it
    state = session.info.pop(_STATE, None)
    if state is None or not state.ids:
        return
    from backend.app.services.mqtt_relay import mqtt_relay

    if not mqtt_relay.enabled or not mqtt_relay.connected:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(mqtt_relay.on_queue_jobs_added(list(state.ids)))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
