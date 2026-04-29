"""Auto-Print G-code Injection — splices operator snippets into plate gcode (#422).

Companion to ``gcode_patcher.py``. Where the patcher comments out specific
vibration-check commands, this module **adds** new G-code lines at two
well-defined anchors:

* **Start snippets** anchor at the ``; MACHINE_START_GCODE_END`` marker that
  Bambu/Orca slicers emit at the bottom of their built-in startup block.
  Bed heat, homing, and nozzle prime are already done at that point — the
  injected snippet runs in the same place a slicer-side custom-start-gcode
  would, so a "set lid LED to red" or "park to back-left" snippet can't
  crash into a not-yet-homed head (A.17 anchor fix).

* **End snippets** are appended after the last gcode line. The printer's
  own end-gcode (cooldown, tool retract) has already executed, so end
  snippets typically run after the print is structurally done.

Snippets support ``{placeholder}`` substitution against values parsed from
the 3MF gcode-header block (``; HEADER_BLOCK_START`` … ``; HEADER_BLOCK_END``).
The substitution map is keyed on lower-cased + space-to-underscore-normalised
header keys, with Prusa→Bambu aliases so snippets copy-pasted from
PrusaSlicer libraries (``{max_layer_z}``) resolve against Bambu's
``max_z_height`` (A.17 placeholder fix). Without this alias map a literal
``Z{max_layer_z}`` reaches the printer as ``Z0`` and the head crashes.

The injector operates on a **temp copy** like the patcher; the source 3MF
is never modified so dedup hash math stays sane.
"""

from __future__ import annotations

import logging
import re
import tempfile
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)


# Header keys that PrusaSlicer emits but Bambu/Orca don't — map to the
# Bambu equivalent so snippets work across both ecosystems unchanged.
_HEADER_PLACEHOLDER_ALIASES = {
    "max_layer_z": "max_z_height",
    "max_print_height": "max_z_height",
    "total_layers": "total_layer_number",
}

# Matches `; key : value` lines inside the HEADER_BLOCK. Tolerant of extra
# whitespace and `[units]` suffixes on the key (stripped during parse).
_HEADER_KEY_RE = re.compile(r"^;\s*([^:]+?)\s*:\s*(.+?)\s*$")
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_START_GCODE_END_MARKER = "; MACHINE_START_GCODE_END"


def _parse_3mf_gcode_header(content: str) -> dict[str, str]:
    """Parse the ``; HEADER_BLOCK_START``…``; HEADER_BLOCK_END`` block.

    Keys are lowercased, ``[units]`` suffixes stripped, and spaces converted
    to underscores so callers can look up ``total_layer_number`` regardless
    of whether the source line is ``; total layer number: 80`` or
    ``; total filament length [mm] : 12155.34``.
    """
    header: dict[str, str] = {}
    in_header = False
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line == "; HEADER_BLOCK_START":
            in_header = True
            continue
        if line == "; HEADER_BLOCK_END":
            break
        if not in_header:
            continue
        m = _HEADER_KEY_RE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        key = re.sub(r"\s*\[[^\]]*\]\s*$", "", key)
        key = key.strip().lower().replace(" ", "_")
        header[key] = value
    return header


def _substitute_placeholders(snippet: str, header: dict[str, str]) -> str:
    """Replace ``{var}`` with the matching header value, leaving unknowns intact."""

    def repl(m: re.Match) -> str:
        name = m.group(1)
        value = header.get(name)
        if value is None:
            alias = _HEADER_PLACEHOLDER_ALIASES.get(name)
            if alias is not None:
                value = header.get(alias)
        if value is None:
            logger.warning(
                "G-code injection: placeholder {%s} not found in 3MF header; leaving as-is",
                name,
            )
            return m.group(0)
        return value

    return _PLACEHOLDER_RE.sub(repl, snippet)


def _inject_start_at_marker(content: str, snippet: str) -> str:
    """Insert ``snippet`` at the start of the line containing the marker.

    Splices BEFORE the marker line so the marker comment stays visible in
    the resulting gcode. Falls back to prepending the snippet to the whole
    file if the marker isn't present (older 3MFs / non-Bambu slicers) — a
    diagnostic warning is logged so the operator can see why a snippet
    landed outside the expected anchor.
    """
    marker_idx = content.find(_START_GCODE_END_MARKER)
    if marker_idx == -1:
        logger.warning(
            "G-code injection: '%s' not found, prepending start snippet to whole file",
            _START_GCODE_END_MARKER,
        )
        return snippet.rstrip("\n") + "\n" + content
    line_start = content.rfind("\n", 0, marker_idx)
    line_start = 0 if line_start == -1 else line_start + 1
    return content[:line_start] + snippet.rstrip("\n") + "\n" + content[line_start:]


def inject_gcode_into_3mf(
    source_path: Path,
    plate_id: int,
    start_gcode: str | None,
    end_gcode: str | None,
) -> Path | None:
    """Create a temp copy of a 3MF with injected start/end snippets for one plate.

    Returns the temp path on success, or None if (a) both snippets are empty,
    (b) the source has no plate gcode files, or (c) any error occurs. Caller
    owns the temp file and is responsible for cleanup.
    """
    if not start_gcode and not end_gcode:
        return None

    tmp_path: Path | None = None
    try:
        with zipfile.ZipFile(source_path, "r") as zf:
            all_gcode = [f for f in zf.namelist() if f.endswith(".gcode")]
            if not all_gcode:
                return None

            target_gcode: str | None = None
            plate_pattern = f"plate_{plate_id}.gcode"
            for f in all_gcode:
                if f.endswith(plate_pattern):
                    target_gcode = f
                    break
            if target_gcode is None:
                target_gcode = all_gcode[0]

            gcode_content = zf.read(target_gcode).decode("utf-8", errors="ignore")
            header = _parse_3mf_gcode_header(gcode_content)

            if start_gcode:
                resolved = _substitute_placeholders(start_gcode, header)
                gcode_content = _inject_start_at_marker(gcode_content, resolved)
            if end_gcode:
                resolved = _substitute_placeholders(end_gcode, header)
                gcode_content = gcode_content.rstrip("\n") + "\n" + resolved + "\n"

            with tempfile.NamedTemporaryFile(delete=False, suffix=".3mf") as tmp:
                tmp_path = Path(tmp.name)

            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf_write:
                for item in zf.namelist():
                    info = zf.getinfo(item)
                    if item == target_gcode:
                        zf_write.writestr(info, gcode_content.encode("utf-8"))
                    else:
                        zf_write.writestr(info, zf.read(item))
        return tmp_path
    except Exception as exc:
        logger.warning("G-code injection failed for %s plate %s: %s", source_path, plate_id, exc)
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        return None
