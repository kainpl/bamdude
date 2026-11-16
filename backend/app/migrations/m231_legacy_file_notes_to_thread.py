"""A library file's legacy ``notes`` text moves into its notes thread (G11, owner 2026-11-16).

``library_files.notes`` is Bambuddy's single text field. The API still wrote and
returned it, but no screen showed it since the notes thread
(``library_file_notes``, authors and all) replaced it — whatever an installation
carried there was invisible. This moves that text into the thread and the API
stops offering the field; the column itself stays in the table (no recreate),
unused.

Per file with a non-blank ``notes``:

* one thread note with that text — or several, in reading order, when the text
  is longer than a note may be (the thread's body is 1000 characters and
  PostgreSQL enforces it; nothing is cut off);
* its author is the file's owner when that user still exists, else nobody;
* its time is the file's ``updated_at``, else ``created_at``;
* and the legacy column is emptied in the same transaction, which is what makes
  a second run insert nothing: the text exists once, in the thread.

Tables and columns are named here: an ORM model would bring today's columns into
a step of the chain where they may not exist.
"""

from datetime import datetime, timezone

version = 231
name = "legacy_file_notes_to_thread"

NOTE_MAX = 1000


def split_note(text: str, limit: int = NOTE_MAX) -> list[str]:
    """``text`` in pieces of at most ``limit`` characters, cut at whitespace where there is any."""
    pieces: list[str] = []
    rest = text.strip()
    while len(rest) > limit:
        cut = max(rest.rfind("\n", 0, limit + 1), rest.rfind(" ", 0, limit + 1))
        if cut <= 0:
            cut = limit
        piece, rest = rest[:cut].rstrip(), rest[cut:].lstrip()
        if piece:
            pieces.append(piece)
    if rest:
        pieces.append(rest)
    return pieces


async def upgrade(conn):
    """No schema change; the legacy text is moved by seed."""


async def seed(session_factory):
    from sqlalchemy import DateTime, Integer, String, Text, column, insert, select, table, update

    files = table(
        "library_files",
        column("id", Integer),
        column("notes", Text),
        column("created_by_id", Integer),
        column("created_at", DateTime),
        column("updated_at", DateTime),
    )
    notes = table(
        "library_file_notes",
        column("library_file_id", Integer),
        column("user_id", Integer),
        column("body", String),
        column("created_at", DateTime),
        column("updated_at", DateTime),
    )
    users = table("users", column("id", Integer))

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(
                    files.c.id,
                    files.c.notes,
                    files.c.created_by_id,
                    files.c.created_at,
                    files.c.updated_at,
                )
                .where(files.c.notes.is_not(None))
                .order_by(files.c.id)
            )
        ).all()
        if not rows:
            return
        # SQLite runs no FK action: a file may still name a user who is gone.
        known_users = set((await db.execute(select(users.c.id))).scalars().all())
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for file_id, text, owner_id, created_at, updated_at in rows:
            when = updated_at or created_at or now
            # Newest first is how the thread reads (time, then id): the first
            # piece is written last, so a long text still reads top to bottom.
            for piece in reversed(split_note(text or "")):
                await db.execute(
                    insert(notes).values(
                        library_file_id=file_id,
                        user_id=owner_id if owner_id in known_users else None,
                        body=piece,
                        created_at=when,
                        updated_at=when,
                    )
                )
            await db.execute(update(files).where(files.c.id == file_id).values(notes=None))
        await db.commit()
