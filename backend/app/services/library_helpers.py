"""Pure helpers for normalising LibraryFile attributes.

Both ``detect_file_type`` and ``compute_file_tags`` exist so the seven
``LibraryFile()`` construction sites and the m035 / m036 migrations all
derive identical values from identical inputs. Any new detection rule
lands here, never inline in routes — that's how this codebase ended up
with three different ``.gcode.3mf`` interpretations in the first place
(naive split in upload, compound recognition in external scan,
hardcoded ``"gcode"`` in slicer-output).
"""

from __future__ import annotations

import os
from datetime import datetime

_SLICED_3MF_SUFFIX = ".gcode.3mf"


def detect_file_type(filename: str) -> str:
    """Return the canonical ``library_files.file_type`` value for ``filename``.

    Single value, lower-cased, no compound representations:

    - ``foo.gcode.3mf`` → ``"gcode"``  (sliced 3MF zip with embedded G-code;
      same primary type as raw .gcode so the file-manager renders one
      "Print" affordance regardless of container shape)
    - ``foo.3mf``       → ``"3mf"``    (project / unsliced)
    - ``foo.gcode``     → ``"gcode"``  (raw G-code)
    - ``foo.stl``       → ``"stl"``
    - ``foo.step``      → ``"step"``
    - ``foo.stp``       → ``"stp"``
    - anything else     → ``"unknown"``

    The primary file_type stays singular for backward-compat (filter
    dropdown, FTS, Telegram bot). Composite identity ("this is a sliced
    3MF, not a raw .gcode") is exposed separately via
    :func:`compute_file_tags`.
    """
    lower = filename.lower()
    if lower.endswith(_SLICED_3MF_SUFFIX):
        return "gcode"
    ext = os.path.splitext(lower)[1]
    if not ext:
        return "unknown"
    return ext[1:]


def compute_file_tags(
    *,
    filename: str,
    file_type: str,
    file_metadata: dict | None,
    source_type: str | None,
    swap_compatible: bool,
) -> list[str]:
    """Composite tag list driving frontend badges + chip-row filter.

    Emission order here is grouped by semantics and is NOT the visual
    display order — the frontend's ``sortTagsForDisplay`` helper sorts
    by an explicit precedence list before rendering, so adjusting how
    the row reads is a one-file frontend change.

    Tag groups emitted:

    - **Format** chips (one per file extension; ``.gcode.3mf`` gets the
      composite ``gcode`` + ``3mf`` pair so the sliced container is
      visually distinct from a raw ``.gcode``; ``.stp`` collapses to
      the ``step`` chip).
    - **Readiness / state** chips, mutually exclusive in practice:
      ``sliced`` (BamDude sidecar output), ``project`` (unsliced
      ``.3mf`` package), ``geometry`` (raw mesh / CAD — STL / OBJ /
      STEP / STP).
    - **Structural modifiers**: ``multiplate``, ``swap``.
    - **Provenance**: ``makerworld``.

    Note: ``project`` is no longer a provenance tag — m037 retired the
    source-based ``project_*`` rule (near-empty hit rate) and re-purposed
    the name for the file-type semantic above. ``sliced`` is no longer
    grouped with provenance either (it answers the same "is it ready
    to print" question as ``project`` / ``geometry``).

    All inputs are taken explicitly so the m036/m037 backfill migrations
    can reuse the helper exactly as the runtime write paths do.
    """
    tags: list[str] = []
    lower_name = filename.lower()
    is_sliced_3mf = lower_name.endswith(_SLICED_3MF_SUFFIX)
    meta = file_metadata or {}

    # Format chip(s).
    if file_type == "gcode":
        tags.append("gcode")
        if is_sliced_3mf:
            tags.append("3mf")  # composite — sliced 3MF carries both
    elif file_type == "3mf":
        tags.append("3mf")
    elif file_type == "stl":
        tags.append("stl")
    elif file_type == "obj":
        tags.append("obj")
    elif file_type in ("step", "stp"):
        tags.append("step")
    # Anything else (txt, gif, image…) gets no format tag.

    # Readiness / state — mutually exclusive in practice. ``sliced``
    # wins over the file-type-derived ``project`` / ``geometry`` because
    # the source_type signal is more specific (a sliced .3mf is no
    # longer a project).
    if source_type in ("sliced", "archive"):
        # ⚠️ A file saved out of a print archive is sliced by definition — it was
        # printed. Without this it would fall through to no readiness tag at all,
        # because ``detect_file_type`` collapses ``.gcode.3mf`` to ``gcode`` and
        # the ``project`` branch below only catches a bare ``3mf``.
        tags.append("sliced")
    elif file_type == "3mf":
        # ``detect_file_type`` already collapses sliced .gcode.3mf to
        # ``"gcode"``, so file_type == "3mf" here means the row is an
        # unsliced project package.
        tags.append("project")
    elif file_type in ("stl", "obj", "step", "stp"):
        tags.append("geometry")

    # Structural modifiers.
    if meta.get("is_multi_plate") or len(meta.get("plates") or []) > 1:
        tags.append("multiplate")
    if swap_compatible:
        tags.append("swap")

    # Provenance.
    if source_type == "makerworld":
        tags.append("makerworld")

    return tags


async def sync_system_tags(db, file) -> list[str]:
    """Derive this file's system tags and write BOTH representations.

    The only writer of either. ``file_tags`` is the cache the hot path reads
    (the badge row, ``isSliced`` on the frontend, preview-tab visibility in
    ``routes/library.py``); the association rows in ``library_file_tags`` are
    what the catalog counts and the ``tag_ids`` filter queries. One function
    writing both at one moment is what makes keeping two representations safe —
    the moment there is a second writer they can disagree, and a file whose
    badges say "STL" while every filter says it does not exist is a bug nobody
    reports because nothing looks broken.

    In practice this is always an insert: nothing re-derives ``file_tags`` for
    an existing file today, and renaming one does not either. It is written as a
    reconcile anyway, because the m128 backfill calls it and because a future
    re-derive path would otherwise become a second definition of what a system
    tag means.

    Returns the codes, so a caller that needs them does not recompute.
    """
    from sqlalchemy import delete, select

    from backend.app.models.library import LibraryFileTag, LibraryTag

    codes = compute_file_tags(
        filename=file.filename,
        file_type=file.file_type,
        file_metadata=file.file_metadata,
        source_type=file.source_type,
        swap_compatible=bool(file.swap_compatible),
    )
    file.file_tags = codes

    if file.id is None:
        # Writing the cache and silently skipping the rows is exactly the drift
        # this function exists to prevent, so fail rather than half-succeed.
        raise ValueError("sync_system_tags needs a flushed file — associations key off file.id")

    wanted = dict(
        (await db.execute(select(LibraryTag.code, LibraryTag.id).where(LibraryTag.is_system.is_(True)))).all()
    )
    wanted_ids = {wanted[code] for code in codes if code in wanted}

    current_ids = set(
        (
            await db.execute(
                select(LibraryFileTag.tag_id)
                .join(LibraryTag, LibraryTag.id == LibraryFileTag.tag_id)
                .where(LibraryFileTag.file_id == file.id, LibraryTag.is_system.is_(True))
            )
        )
        .scalars()
        .all()
    )

    stale = current_ids - wanted_ids
    if stale:
        # Scoped to this file AND to the ids we resolved as system — a broader
        # delete would strip the labels the user applied by hand.
        await db.execute(
            delete(LibraryFileTag).where(LibraryFileTag.file_id == file.id, LibraryFileTag.tag_id.in_(stale))
        )
    missing = wanted_ids - current_ids
    if missing:
        await db.execute(LibraryFileTag.__table__.insert(), [{"file_id": file.id, "tag_id": tid} for tid in missing])

    return codes


def skip_objects_supported_from_metadata(file_metadata: dict | None) -> bool:
    """Whether per-object skipping will work for a file, from stored metadata.

    Mirrors ``services.archive.extract_skip_support_from_3mf`` — which reads the
    same two fields live from the 3MF — so the list badge and the preview banner
    can never contradict each other. Agreement is by construction, not by
    coincidence: ``ThreeMFParser._extract_print_settings`` has already applied
    the "absent ``gcode_label_objects`` means True" Bambu Studio default before
    writing the key, and already omits ``exclude_object`` entirely when the 3MF
    carries no interpretable value. A missing key therefore reads as False here,
    which is the honest answer — without ``exclude_object`` the slicer emits no
    ``M624``/``M625`` and the printer physically cannot exclude anything,
    whatever the object list says.

    Lives beside :func:`compute_file_tags` for the same reason it does: every
    ``LibraryFile()`` construction site and the m114 backfill must derive an
    identical value from identical inputs.
    """
    meta = file_metadata or {}
    return bool(meta.get("gcode_label_objects")) and bool(meta.get("exclude_object"))


def folder_activity_at(folder, latest_file_activity: datetime | None = None) -> datetime:
    """When a folder was last meaningfully touched — the folder-sort key (#1770, #2680).

    Two sources, newest wins: the folder's own timestamp and the newest activity
    among the files it contains. The folder's own timestamp prefers the real
    on-disk mtime (``fs_modified_at``, written by the external scan) and falls
    back to ``updated_at``, which is all we have for internal folders and for
    external ones not yet re-scanned since m129.

    Lives here rather than inline for the reason the rest of this module does:
    seven routes in ``routes/library.py`` answered this question with seven
    copies of the same expression, and adding the on-disk source to six of them
    would have been the kind of half-fix this cycle has already paid for twice.

    Takes ``latest_file_activity`` rather than querying: the callers differ in
    how they get it (one grouped aggregate for the whole tree, a scalar
    aggregate for a single folder, and nothing at all for a folder that was
    just created), and passing it in is what lets all three share the rule.
    """
    own = folder.fs_modified_at or folder.updated_at
    if latest_file_activity is None:
        return own
    return max(own, latest_file_activity)


def project_for_library_file(explicit: int | None, library_file) -> int | None:
    """Which project a print belongs to, when the caller did not name one.

    An operator who names a project always wins. Otherwise the file's own links
    answer: a file already sitting in a project produces prints that sit in the
    same project, without the interface having to re-state it from whichever
    page the print was started on.

    m044 made the link many-to-many while archives, queue items and auto-queue
    items each carry a single project. The first link is taken — deterministic,
    since the pivot reads in insertion order — and an operator who needs a
    different one passes it explicitly.

    Takes an already-loaded file: ``projects`` is a relationship, and touching
    it inside a request that did not eager-load it raises ``MissingGreenlet``.
    Callers use ``selectinload(LibraryFile.projects)``.

    This lived twice, written out in the queue and auto-queue routes, and the
    direct-print route never received a copy — so printing a project-linked
    file straight to a printer produced an archive with no project at all.
    That is why it is one function now.
    """
    if explicit is not None:
        return explicit
    if library_file is None:
        return None
    projects = getattr(library_file, "projects", None) or []
    return projects[0].id if projects else None
