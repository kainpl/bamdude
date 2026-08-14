"""Which files on the printer came from a print of ours, and may be removed.

One upload becomes **several files on the machine**. We send ``Cube.3mf``; the
printer materialises ``Cube.gcode.3mf`` — in ``/cache`` when the job was read
off the card, and at the root of internal storage when "store sent files to
storage" is on (``cfg`` bit 19, the operator's own setting). Measured on a live
X2D across four clean-slate runs.

Post-print cleanup used to look for exactly the name it had uploaded, so it
never saw either copy and reported "nothing to delete" — correctly, by its own
rule. The rule is what was wrong.

⚠️ **The name is a filter; the CONTENT decides.** ``Cube.gcode.3mf`` could just
as easily have been put there by an operator sending from BambuStudio, and
deleting a stranger's file because its name matches a pattern is the one
mistake here that cannot be undone. So a copy is removed only once its bytes
hash to something this print actually produced. The two errors are not
symmetric: a file left behind is visible and reversible, a file wrongly deleted
is neither.

⚠️ **Only for names WE did not upload.** The file at the path the dispatcher
uploaded to needs no proof — we put it there, and reading it back to confirm
would be a download per print for nothing.

This module is deliberately transport-agnostic: the same decision has to serve
the card over FTP and internal storage over the tunnel, and later a retention
sweep whose only difference is what triggers it (age or free space, rather than
the end of a print). See the vault note "Політика зберігання на носіях
принтера".
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

_THREEMF = ".3mf"
_GCODE_THREEMF = ".gcode.3mf"


def derived_copy_names(uploaded_name: str) -> list[str]:
    """The names the printer makes for itself out of one upload of ours.

    ``Cube.3mf`` → ``["Cube.gcode.3mf"]``. A name that is already the derived
    form returns nothing, so feeding this its own output cannot invent
    ``Cube.gcode.gcode.3mf``.

    Accepts a bare name or a path; only the last segment is used, because the
    two media disagree about what a path looks like and the name is the part
    they share.
    """
    name = uploaded_name.rsplit("/", 1)[-1]
    lowered = name.lower()
    if not lowered.endswith(_THREEMF) or lowered.endswith(_GCODE_THREEMF):
        return []
    return [name[: -len(_THREEMF)] + _GCODE_THREEMF]


def archive_hashes(archive: object) -> set[str]:
    """Every digest that identifies this print's bytes.

    Both, and not one: ``source_content_hash`` is the unpatched original and
    ``content_hash`` is what actually went up the wire. They differ exactly when
    a 3MF patch was applied, and the printer's copy is a copy of the bytes it
    received — so which of the two matches depends on whether this print was
    patched, and asking for the wrong one would spare the file for the wrong
    reason.
    """
    found: set[str] = set()
    for attr in ("source_content_hash", "content_hash"):
        value = getattr(archive, attr, None)
        if isinstance(value, str) and value.strip():
            found.add(value.strip().lower())
    return found


async def remove_verified_copies(
    *,
    entries: list[dict],
    wanted: set[str],
    expected_hashes: set[str],
    read_bytes: Callable[[str], Awaitable[bytes | None]],
    delete: Callable[[str], Awaitable[object]],
    label: str,
    allow_unverified: bool = False,
) -> int:
    """Delete the printer's own copies of this print, proving each one first.

    ``entries`` is a listing in the shape both transports emit (``name``,
    ``path``, ``is_directory``). Returns how many were removed.

    ``allow_unverified`` drops the content check when there is nothing to check
    against — a print BamDude picked up rather than sent (from the slicer, or
    started on the printer's own screen) whose 3MF was never recovered, so its
    archive carries no digest. **Only ever pass it for the job that has just
    finished on this machine**, where the name is bound to that job rather than
    merely resembling it. It is a genuinely weaker claim and it is logged as
    one; a sweep across a medium must never set it.

    Best-effort throughout: a copy that cannot be read, cannot be hashed or
    cannot be deleted is left alone and logged. Cleanup failing must never cost
    anybody a print, and the file staying is the safe outcome of every branch.
    """
    if not wanted or not (expected_hashes or allow_unverified):
        return 0

    lowered = {n.lower() for n in wanted}
    removed = 0

    for entry in entries:
        if entry.get("is_directory"):
            continue
        name = entry.get("name") or ""
        if name.lower() not in lowered:
            continue

        path = entry.get("path") or name

        if expected_hashes:
            try:
                data = await read_bytes(path)
            except Exception as exc:  # noqa: BLE001 — an unreachable printer is not our business here
                logger.debug("[CLEANUP] %s: could not read %s: %s", label, path, exc)
                continue
            if not data:
                logger.debug("[CLEANUP] %s: %s read back empty, leaving it", label, path)
                continue

            digest = hashlib.sha256(data).hexdigest()
            if digest not in expected_hashes:
                # ⚠️ The case this whole function exists for: same name,
                # different file. Logged at INFO because it is the interesting
                # outcome — it means somebody else's print is on this machine.
                logger.info(
                    "[CLEANUP] %s: %s has the expected name but not our bytes — left in place",
                    label,
                    path,
                )
                continue
        else:
            logger.info(
                "[CLEANUP] %s: removing %s on its name alone — this print has no stored digest to check it against",
                label,
                path,
            )

        try:
            await delete(path)
        except Exception as exc:  # noqa: BLE001 — same as above
            logger.debug("[CLEANUP] %s: could not delete %s: %s", label, path, exc)
            continue
        removed += 1
        logger.info("[CLEANUP] %s: removed the printer's copy %s", label, path)

    return removed
