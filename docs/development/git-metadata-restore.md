# Git metadata restore

Run **Restore from Git** only while the farm is idle, with no active prints or
uploads. A large SQLite archive category holds the database writer until its
commit; other writers can exceed their busy timeout and fail to save a print's
completion. The warning does not stop the printers or close upload admission.

Git backups contain metadata, not the archived 3MF, thumbnails or source bytes.
New backups carry each archive's execution UUID. Restore uses that identity even
when file attachment has changed the filename or hashes. An incoming different
UUID cannot adopt a record through the filename or hash. Unproven conflicts,
including rows in the trash, are skipped and counted.

Older backups can match only a unique hash plus start, or filename plus a
nonempty start. These are compatibility heuristics. A renamed old print may not
match; old source-only or re-slice records without a UUID, hash or start are
skipped with a separate count. They are never matched by filename alone.

An active or uncertain snapshot cannot replace local results or file metadata.
Only notes, tags, favourites and external links may change. A positively
identified new execution with an unknown result is restored as cancelled,
marked uncertain, with no invented usage, cost, energy or completion time.
Restore never sends completion commands or replays stock or spool accounting.
Local paths, existing printer identity and order membership are preserved.

Missing fields do not mean null. Normal terminal metadata follows field
presence and type rules. Energy and its provenance update together; old measured
numbers, including zero, retain their meaning. Missing energy stays missing.
An incomplete overwrite leaves the whole local energy bundle intact.

Select both archive and spool categories to preserve print links in usage
history. The collector's history key and the legacy usage_history alias are
accepted; conflicting copies refuse the spool category before it writes. Links
use the accepted archive mapping and recheck its UUID after the archive commit.
Restoring history never changes live spool weight.

Each database category commits separately. A later failure leaves earlier
committed categories in place and reports them. This is not a full database
rollback or a guarantee that a running farm can safely restore concurrently.
