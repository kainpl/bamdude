"""The one seam a caller threads through a queue writer to record its own receipt atomically with the jobs.

Both writers publish their rows inside ``queue_sources.publish``: under the storage guard, in one transaction that
``publish_staged`` commits. A receipt written AFTER the writer returned would leave a crash window -- jobs committed,
receipt not. So a caller that must prove "these exact jobs were created once" passes a :class:`PublicationReceipt`.
The writer calls it inside that very transaction (OrcaBamDude spec r2 §8.3, A1):

- ``claim(session, source)`` -- under the writer's guards (the printer queue's scope lock already held), before any
  job row, with the published :class:`QueueSource` (its ``sha256`` names the captured bytes). It re-reads whatever
  it must and refuses by raising: :class:`AlreadyPublished` when another publication of the
  same work committed first, or its own refusal. A raise rolls the whole publication back -- no job row, no counter,
  no new source row.
- ``record(session, queue_item_ids=…, auto_item_ids=…)`` -- after the rows are flushed, with their exact ids, in the
  same transaction. Its write commits with the jobs or not at all.

Never "return from ``attach`` without rows": the publication would commit a source with nothing attached. A caller
that passes no receipt gets exactly the old behaviour.
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.queue_source import QueueSource


class AlreadyPublished(Exception):
    """Another publication of the same work committed first; this one is rolled back and must read that result."""


class PublicationReceipt(Protocol):
    async def claim(self, session: AsyncSession, source: QueueSource) -> None: ...

    async def record(self, session: AsyncSession, *, queue_item_ids: list[int], auto_item_ids: list[int]) -> None: ...
