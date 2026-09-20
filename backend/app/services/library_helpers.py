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

import logging
import os
import zipfile
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_SLICED_3MF_SUFFIX = ".gcode.3mf"

# The key ``compute_file_tags`` prefers over the filename when deciding whether
# a 3MF is sliced. Written by whoever parses the file; absent on rows that
# predate it, which the tag rule handles explicitly.
SLICED_GCODE_META_KEY = "has_sliced_gcode"


def sliced_gcode_in_3mf(path: str | Path) -> bool | None:
    """Does this 3MF actually contain sliced G-code? ``None`` when unreadable.

    The one content check behind the ``gcode`` tag. A Bambu slicer writes
    ``Metadata/plate_N.gcode`` into the container; a project or model export
    does not, whatever it is called.

    ⚠️ Reads only the ZIP **central directory** — ``namelist()`` — and extracts
    nothing. That is what makes it cheap enough to run over a whole library in
    a migration, and it is the same read ``parse_plates_from_3mf`` already does
    to discover plates.

    ⚠️ ``None`` is not ``False``. A file that cannot be opened has not been
    shown to lack G-code, and the caller must fall back rather than record an
    answer it does not have.
    """
    try:
        with zipfile.ZipFile(str(path), "r") as zf:
            return any(n.startswith("Metadata/") and n.endswith(".gcode") for n in zf.namelist())
    except (OSError, zipfile.BadZipFile) as e:
        logger.debug("Could not inspect %s for sliced gcode: %s", path, e)
        return None


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
    meta = file_metadata or {}

    # ⚠️ **Content beats the filename.** ``file_type`` comes from
    # ``detect_file_type(filename)``, so on its own the ``gcode`` tag answers
    # "is it NAMED like a sliced file", not "is it one" — and that tag is what
    # gates every "can this be printed" affordance. ``sliced_gcode_in_3mf``
    # looks inside the container; whoever parsed the file leaves the answer
    # here.
    #
    # ⚠️ Absent means unknown, NOT false. Three migrations (m036, m037, m041)
    # call this helper from stored metadata and never open a file, and rows
    # written before the key existed have no answer either. Both fall back to
    # the filename rule, which is what they were built on.
    # ⚠️ ``effective_type`` is resolved ONCE and drives both the format chip and
    # the readiness chip below. Deriving them separately is how a file could end
    # up tagged ``gcode`` and ``project`` at the same time — contradictory, and
    # invisible until something filtered on one of them.
    sliced_by_content = meta.get(SLICED_GCODE_META_KEY)
    effective_type = file_type
    if sliced_by_content is not None and file_type in ("gcode", "3mf"):
        # Only a 3MF container can be re-judged: the key is written by the 3MF
        # parse, and a raw ``.gcode`` never carries it.
        effective_type = "gcode" if sliced_by_content else "3mf"

    # Format chip(s).
    if effective_type == "gcode":
        tags.append("gcode")
        # Composite: a sliced 3MF carries both chips. Gated on the container
        # being a 3MF at all — a raw ``.gcode`` is sliced by definition and is
        # still not a 3MF, whatever the content key says.
        if lower_name.endswith(".3mf"):
            tags.append("3mf")
    elif effective_type == "3mf":
        tags.append("3mf")
    elif effective_type == "stl":
        tags.append("stl")
    elif effective_type == "obj":
        tags.append("obj")
    elif effective_type in ("step", "stp"):
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
    elif effective_type == "3mf":
        # ``detect_file_type`` already collapses sliced .gcode.3mf to
        # ``"gcode"``, so ``3mf`` here means the row is an unsliced project
        # package — by content where we know it, by name where we do not.
        tags.append("project")
    elif effective_type in ("stl", "obj", "step", "stp"):
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
