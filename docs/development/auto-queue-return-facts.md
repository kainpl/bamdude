# Automatic return of failed AutoQueue jobs

`auto_queue_return_failed` is a system opt-in, initially false. It permits at most
two successful automatic returns per immutable logical AutoQueue identity, no
earlier than 120 seconds after the addressed physical attempt ended. Explicit
null is rejected; turning the switch off fences new admissions through a shared
Settings-row lock. It does not cancel an already committed return.

Only the normal live completion can supply settlement: the exact job/archive
terminal commit succeeded, the late archive writer succeeded, the normal body
reached its tail and the awaited leftovers pass succeeded. Critical failure or
cancellation does not grant proof. Reconciliation of an old failed print cannot
mint a receipt. If a disconnect/restart is followed only by reconciliation, the
job remains failed. A durable ready receipt written before restart can still be
consumed after fresh checks.

## Facts and the final writer

AutoQueueReturn records immutable attempt, archive, logical job, printer and
owner identities, source/policy basis, completion time and dispatch evidence.
Numeric ids are navigation hints. Ready and returned are publication facts,
not another print lifecycle. A unique attempt key and a unique logical-job
ordinal make duplicate callbacks/ticks idempotent. Backup and portable transfer
retain these facts; additive migration never synthesizes legacy receipts.

The publisher and final writer verify accepted bytes outside SQL writer locks,
then enter storage mutation, clean session, archive-before-lane guards and
current owner/order/row checks. PostgreSQL toggle reads use FOR SHARE. The final
commit copies irreversible proof before clearing assignment, returns the same
ancestor to pending, increments its count, stamps the receipt and marks the old
printer row. Uncertain commit acknowledgement is resolved by guarded reread;
there is no compensating create/delete or second increment.

The old status, physical failure code/message, per-print archive/cost, source
ownership, pause, plate gate and SWAP debt remain intact. A macro claim that is
owed/attempted/sending refuses return; a known failed-print hold alone does not.
Retry, Clone and Repeat share the returned refusal, including missing receipt
references. Clear and ordinary Delete retain their existing addressed behavior.

## Routing and operator controls

One pure rank is shared by actual routing and rebalance replay. Busy printers
remain excluded before filament fit; no new wait-for-busy rule is introduced.
The last failed printer is ranked last by immutable identity, then ordinary
readiness/model/colour/id ties apply. The existing previous-success requirement
still excludes a printer whose last print failed. A sole eligible failed printer
may receive pending work, which waits for Resume.

A new never-started attempt may be moved back to AutoQueue with its logical
identity, count and proof unchanged. Policy edits preserve them too. Model
rebalance skips returned rows both at selection and final claim, including force;
its shared refusal remains open to explicit order/plate-fit replanning. Such a
reviewed replacement is new logical work, with its own budget and retained old
print history.

## Runtime and views

A bounded keyset pass runs before the ordinary router opens its session. One
owner retains admitted publication/pass/file I/O/SQL through actual exit when a
caller is cancelled. Restore closes admission before awaiting drain and refuses
storage replacement if drain is not proven; the existing farm restore hold keeps
it closed until restart. Startup opens only after startup.complete. Shutdown
keeps unresolved work owned and admission closed.

The pass exits on a disabled setting before reading receipts or taking a writer.
It excludes persisted final refusals (exhausted budget, obsolete attempt or
execution, changed history/settlement/source basis, or an invalid order link).
These receipts remain diagnostic history, not scheduled work. Cheap read-only
checks reject obsolete attempts before hashing or lane locks; the final writer
still repeats its guarded admission checks. An exhausted job creates no new
ready receipt. Temporary refusals remain retryable, and an unchanged diagnostic
episode does not reserve another writer.

Refusals use finite reason codes, deduplicated per receipt/reason episode, apart
from physical error_code. Queue views show return counts, historical returned
markers and deferred reasons without exposing source basis or owner credentials.
Connector observations validate receipt/lineage before retiring an assignment,
prefer a proven current pending ancestor, and retain scoped per-print history.
