"""A job's lineage, written where an execution starts (host spec r2.2 §11 S2).

``stamp_execution`` copies the identities of the queue row a dispatch launches onto the archive of that execution, in
the transaction that creates the archive. Copying from the launched row -- never from a reprint's source archive --
is what lets a reader follow receipt -> AutoQueue job -> queue row -> execution by UUIDs alone. The only other writer
is the AutoQueue promotion, which passes ``source_auto_identity`` to the queue row it creates;
``tests/unit/services/test_job_lineage.py`` fails on any third.
"""

from __future__ import annotations


def stamp_execution(archive, queue_item) -> None:
    archive.queue_item_identity = queue_item.recovery_identity
    archive.source_auto_identity = queue_item.source_auto_identity
