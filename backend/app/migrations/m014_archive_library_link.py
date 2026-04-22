"""Link print_archives to library_files + backfill usage stats.

Adds ``print_archives.library_file_id`` (nullable FK, ON DELETE SET NULL) so
each archive row records which library file it was dispatched from. Going
forward, BamDude dispatch populates this at archive creation time; retro-
active downloads (``attach_3mf_to_archive``) hash-match into it too.

The seed then:

1. Backfills ``library_file_id`` on existing archives by matching
   ``COALESCE(source_content_hash, content_hash)`` against
   ``library_files.file_hash``. Oldest matching library row wins when
   multiple files share a hash — that's the one most likely the true
   origin.
2. Recomputes ``library_files.print_count`` and ``last_printed_at`` from
   the archive history. Only archives with ``status='completed'`` count
   toward print_count — failed, cancelled and aborted prints stay out,
   matching the live-update policy in ``_bump_library_file_usage``. Any
   pre-existing value on a library row is overwritten with the derived
   count, so manual fixups from before this migration are discarded —
   the archive history is the authoritative source.

Safe to re-run: ``add_column`` is idempotent and the seed uses INSERT-less
UPDATE statements that converge on the same state.
"""

from sqlalchemy import text

from backend.app.migrations.helpers import add_column

version = 14
name = "archive_library_link"


async def upgrade(conn):
    await add_column(
        conn,
        "print_archives",
        "library_file_id INTEGER REFERENCES library_files(id) ON DELETE SET NULL",
    )


async def seed(session_factory):
    async with session_factory() as db:
        # Step 1: backfill library_file_id on unlinked archives by hash.
        # Single UPDATE using a correlated subquery — picks the OLDEST
        # library row per hash so reimports don't steal attribution.
        await db.execute(
            text(
                """
                UPDATE print_archives
                SET library_file_id = (
                    SELECT lf.id
                    FROM library_files lf
                    WHERE lf.file_hash IS NOT NULL
                      AND lf.file_hash = COALESCE(
                          print_archives.source_content_hash,
                          print_archives.content_hash
                      )
                    ORDER BY lf.created_at ASC, lf.id ASC
                    LIMIT 1
                )
                WHERE print_archives.library_file_id IS NULL
                  AND COALESCE(
                      print_archives.source_content_hash,
                      print_archives.content_hash
                  ) IS NOT NULL
                """
            )
        )

        # Step 2: recompute print_count + last_printed_at from completed archives.
        # Reset every library file first so rows with no successful prints
        # drop back to zero (e.g. if an archive was deleted since).
        await db.execute(text("UPDATE library_files SET print_count = 0, last_printed_at = NULL"))

        await db.execute(
            text(
                """
                UPDATE library_files
                SET
                    print_count = COALESCE((
                        SELECT COUNT(*)
                        FROM print_archives pa
                        WHERE pa.library_file_id = library_files.id
                          AND pa.status = 'completed'
                    ), 0),
                    last_printed_at = (
                        SELECT MAX(COALESCE(pa.completed_at, pa.created_at))
                        FROM print_archives pa
                        WHERE pa.library_file_id = library_files.id
                          AND pa.status = 'completed'
                    )
                WHERE EXISTS (
                    SELECT 1
                    FROM print_archives pa
                    WHERE pa.library_file_id = library_files.id
                      AND pa.status = 'completed'
                )
                """
            )
        )

        await db.commit()
