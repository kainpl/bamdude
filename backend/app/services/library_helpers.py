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
    if source_type == "sliced":
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
