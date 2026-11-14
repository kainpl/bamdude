# AutoQueue failed-return facts

The schema foundation records settlement and return facts separately from print
history. It does not enable automatic retry or publish a settlement receipt.
The runtime publisher, final return transaction and UI are separate implementation
stages; this foundation must not be released as the completed feature. The shared
repeatability guard refuses any non-null returned marker, including a missing or
malformed receipt reference. Plate ownership remains available to Clear; Repeat
button projections use the same policy as the final rearm writers.

`AutoQueueItem.failed_return_count` belongs to the logical job. Its nullable
`failed_return_proof` preserves dispatch evidence when an eventual guarded
return clears active assignment fields. A new job starts with zero returns and
no proof. A manual transfer of the same job must retain both facts.

`PrintQueueItem.returned_receipt_identity` is separate from `error_code` and
archive failure details. A return must retain the failed printer row, archive,
per-print costs and plate/SWAP evidence. These columns do not authorize reuse of
the failed row. Retry/Clone/Repeat and bulk retry use the shared repeatability
guard. The irreversible AutoQueue proof or a positive count also makes source
declaration refresh ineligible after assignment fields have been cleared.

`AutoQueueReturn` stores immutable job, execution-archive, AutoQueue, printer and
owner identities, accepted-source basis and dispatch evidence. Numeric ids are
navigation hints, without cascading foreign keys: deleting/replacing a job or
reusing its SQLite id cannot erase or reassign an old receipt. This does not
permit a writer to authorize an operation using a receipt's numeric ids.

A ready receipt has no return ordinal or return timestamp. Publication must
set a positive ordinal and `returned_at` together. The database enforces one
receipt per immutable attempt and one published ordinal per logical job. NULL
ordinals remain distinct on SQLite and PostgreSQL. The lookup index has a name
different from both UNIQUE constraints, including through SQLite reflection
and PostgreSQL portable restore.

The additive migration initializes legacy jobs with no settlement proof and
creates no receipts. It seeds the future opt-in setting as disabled without
overwriting an existing value. A rerun retains proofs, markers and counters.
Full backups retain these facts; migration/restore never infer new readiness
from a failed status or an old completion timestamp. Reconciliation-only
failures without live settlement proof will remain failed.
