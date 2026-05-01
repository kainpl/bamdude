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

    Order is stable — tags appear left-to-right in the UI in the order
    emitted here:

    1. **Format tags** (visual primary identity): ``3mf`` / ``gcode`` /
       ``stl`` / ``step``. Sliced 3MFs get both ``gcode`` and ``3mf``
       (composite badge), raw .gcode files get just ``gcode``.
    2. **Structural tags**: ``multiplate``, ``swap``.
    3. **Provenance tags**: ``sliced``, ``makerworld``, ``project``.

    All inputs are taken explicitly so the m036 backfill migration can
    reuse the helper exactly as the runtime write paths do.
    """
    tags: list[str] = []
    lower_name = filename.lower()
    is_sliced_3mf = lower_name.endswith(_SLICED_3MF_SUFFIX)

    # Format tags
    if file_type == "gcode":
        tags.append("gcode")
        if is_sliced_3mf:
            tags.append("3mf")  # composite — sliced 3MF carries both
    elif file_type == "3mf":
        tags.append("3mf")
    elif file_type == "stl":
        tags.append("stl")
    elif file_type in ("step", "stp"):
        tags.append("step")
    # Anything else (txt, gif, model, image…) gets no format tag —
    # callers can still surface the raw file_type as a fallback badge.

    # Structural tags
    meta = file_metadata or {}
    if meta.get("is_multi_plate") or len(meta.get("plates") or []) > 1:
        tags.append("multiplate")
    if swap_compatible:
        tags.append("swap")

    # Provenance tags
    if source_type == "sliced":
        tags.append("sliced")
    elif source_type == "makerworld":
        tags.append("makerworld")
    elif source_type and source_type.startswith("project_"):
        tags.append("project")

    return tags
