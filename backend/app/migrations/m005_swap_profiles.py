"""Introduce swap-mode profiles - second dimension on Printer + Macro.

See :mod:`backend.app.core.swap_profiles` for the profile catalog.

Prior to this migration a printer had a single boolean ``swap_mode_enabled``
and a macro had a single ``swap_mode_only`` flag - so there could only be one
set of swap macros per printer model. In practice multiple hardware revisions
(two A1 Mini variants - Kit Edition and STL Edition - plus the full-size
A1 JobOx rig) need distinct gcode, which this migration unblocks.

Schema:
    * ``printers.swap_profile VARCHAR(50) NULL``
    * ``macros.swap_profile VARCHAR(50) NULL``

Seed:
    * The two A1 Mini built-in swap macros shipped in m001
      (``swap_mode_start`` + ``swap_mode_change_table`` with
      ``swap_mode_only=True`` and ``printer_models=["A1 Mini"]``) are rebound
      to ``swap_profile='a1mini_kit'`` so existing A1 Mini installs keep
      firing the same gcode they have today.
    * Any printer that currently has ``swap_mode_enabled=True`` and model
      ``A1 Mini`` gets ``swap_profile='a1mini_kit'``. Behaviour preserved.
    * Two **empty** built-in macros are seeded for each new profile
      (``a1mini_stl``, ``jobox-a1``) - one per swap event. Operators fill
      the gcode in via Settings → Macros; the seeds exist so the macros
      table is populated even before anyone opens the editor.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

version = 5
name = "swap_profiles"


async def upgrade(conn):
    """Add swap_profile columns + macro description column."""
    from backend.app.migrations.helpers import add_column

    await add_column(conn, "printers", "swap_profile VARCHAR(50)")
    await add_column(conn, "macros", "swap_profile VARCHAR(50)")
    # ``description`` - free-form note (author attribution, upstream version
    # tag, usage caveats). Rendered as a multi-line textarea in the macro
    # editor so users can jot context without touching the gcode itself.
    await add_column(conn, "macros", "description TEXT")


# JobOx A1 swap block - one iteration extracted from a Chinese A1-clone
# test file dated 20240620. Y=266 is the full-size A1 overflow zone (the
# A1 Mini uses 186.5), so this is definitely a full-size A1 sequence.
_JOBOX_A1_GCODE = (
    ";========start plate change=================\n"
    "G91\n"
    "G380 S3 Z-20 F1200\n"
    "G380 S2 Z75 F1200\n"
    "G380 S3 Z-20 F1200\n"
    "G380 S2 Z75 F1200\n"
    "G380 S3 Z-20 F1200\n"
    "G380 S2 Z75 F1200\n"
    "G380 S3 Z-20 F1200\n"
    "G1 Z5 F1200\n"
    "G90\n"
    "G28 Y\n"
    "G90\n"
    "G1 Y266 F2000\n"
    "G4 P1000\n"
    "G91\n"
    "G380 S2 Z30 F1200\n"
    "G90\n"
    "M211 Y0 Z0\n"
    "G91\n"
    "G90\n"
    "G1 Y50 F1000\n"
    "G1 Y0 F2500\n"
    "G91\n"
    "G380 S3 Z-20 F1200\n"
    "G90\n"
    "G1 Y266 F2000\n"
    "G1 Y43 F2000\n"
    "G1 Y266 F2000\n"
    "G1 Y250 F8000\n"
    "G1 Y266 F8000\n"
    "G1 Y43 F5000\n"
    "G1 Y266 F2000\n"
    "G1 Y250 F8000\n"
    "G1 Y266 F8000\n"
    "G1 Y-2 F7000\n"
    "G1 Y150 F2000"
)


# Built-in macros per profile. Each profile gets one macro per swap event.
# Adding a new profile later is just another dict in this list.
_EMPTY_PROFILE_SEEDS: list[dict] = [
    # A1 Mini - STL Edition (gcode extracted from Cube.swaps.3mf generated
    # by swaplist.app STL edition - sequence tag swap-sequence_v05_20260312).
    {
        "name": "A1 Mini. STL Edition. Start Sequence",
        "description": "swapmod-stl-a1m / swap-sequence_v05_20260312 - initial plate seating before the first print.",
        "printer_models": ["A1 Mini"],
        "swap_profile": "a1mini_stl",
        "event": "swap_mode_start",
        "gcode": (
            ";ini swapmod-stl-a1m start / swap-sequence_v05_20260312\n"
            "G90\n"
            "G28\n"
            "G0 Z30 F5000\n"
            "G0 X-10\n"
            "G0 Y-6 F2000\n"
            "G0 Y150\n"
            "G0 Y100\n"
            "G0 Y186.5\n"
            "G0 Y-6\n"
            "G4 S1\n"
            "G0 Y5 F500\n"
            "G0 Y100 F5000"
        ),
    },
    {
        "name": "A1 Mini. STL Edition. Change Table",
        "description": "swapmod-stl-a1m / swap-sequence_v05_20260312 - swaps the finished plate out for a fresh one between prints.",
        "printer_models": ["A1 Mini"],
        "swap_profile": "a1mini_stl",
        "event": "swap_mode_change_table",
        "gcode": (
            ";swap swapmod-stl-a1m start / swap-sequence_v05_20260312\n"
            "G0 X170 F5000\n"
            "G0 Z180 F2000\n"
            "G0 Y186.5 F3000\n"
            "G0 Z186 F2000\n"
            "G0 X188 F5000\n"
            "G0 Z180\n"
            "G4 S1\n"
            "G0 Y150 F200\n"
            "G0 Y-6 F2000\n"
            "G0 Z186 F5000\n"
            "G0 X170\n"
            "G0 Z180 F5000\n"
            "G0 Y150 F2000\n"
            "G0 Y15 F3000\n"
            "G0 Y180 F2000\n"
            "G0 Y186.5 F500\n"
            "G0 Y5 F5000\n"
            "G0 Y-6 F200\n"
            "G4 S1\n"
            "G0 Y5 F500\n"
            "G0 Y100 F5000"
        ),
    },
    # Full-size A1 - JobOx (gcode extracted from a Chinese A1-clone test file;
    # the JobOx official sequence is not public. Same gcode is used for both
    # start + change_table events because the clone test file repeated one
    # identical block 20× and gave no separate "initial seating" routine.
    # Operators with an actual JobOx rig should override these via the macro
    # editor once they confirm a safer initial sequence.)
    {
        "name": "JobOx A1. Start Sequence",
        "description": "Extracted from A1-clone test file (Y=266 full-size overflow). Review before first run on real hardware.",
        "printer_models": ["A1"],
        "swap_profile": "jobox-a1",
        "event": "swap_mode_start",
        "gcode": _JOBOX_A1_GCODE,
    },
    {
        "name": "JobOx A1. Change Table",
        "description": "Extracted from A1-clone test file (Y=266 full-size overflow). Same sequence as start until real JobOx gcode is confirmed.",
        "printer_models": ["A1"],
        "swap_profile": "jobox-a1",
        "event": "swap_mode_change_table",
        "gcode": _JOBOX_A1_GCODE,
    },
]


async def seed(session_factory):
    """Backfill existing data + seed empty macros for the new profiles."""
    from sqlalchemy import select

    from backend.app.models.macro import Macro
    from backend.app.models.printer import Printer

    async with session_factory() as db:
        # 1. Rebind existing built-in A1 Mini swap macros to the Kit Edition
        #    profile - that's the variant users who shipped with m001 already
        #    have in their database. STL Edition gets its own seed rows
        #    below, so we don't clobber custom multi-model macros here.
        result = await db.execute(
            select(Macro).where(
                Macro.swap_mode_only.is_(True),
                Macro.event.in_(["swap_mode_start", "swap_mode_change_table"]),
                Macro.swap_profile.is_(None),
            )
        )
        macros = list(result.scalars().all())
        rebound = 0
        for macro in macros:
            try:
                models = json.loads(macro.printer_models or "[]")
            except (json.JSONDecodeError, TypeError):
                models = []
            if models == ["A1 Mini"]:
                macro.swap_profile = "a1mini_kit"
                rebound += 1

        if rebound:
            logger.info("m005: rebound %d A1 Mini swap macro(s) to swap_profile=a1mini_kit", rebound)

        # 2. Preserve current behaviour for printers that already have swap on.
        result = await db.execute(
            select(Printer).where(
                Printer.swap_mode_enabled.is_(True),
                Printer.swap_profile.is_(None),
            )
        )
        printers = list(result.scalars().all())
        migrated = 0
        for printer in printers:
            if printer.model == "A1 Mini":
                printer.swap_profile = "a1mini_kit"
                migrated += 1
            # A1 / other models are intentionally left null - the admin
            # picks a profile explicitly in the UI after m005 lands.

        if migrated:
            logger.info("m005: migrated %d A1 Mini printer(s) to swap_profile=a1mini_kit", migrated)

        # 3. Seed empty built-in macros for the remaining profiles.
        #    Idempotent: existence is checked by (swap_profile, event).
        seeded = 0
        for spec in _EMPTY_PROFILE_SEEDS:
            existing = (
                await db.execute(
                    select(Macro).where(
                        Macro.swap_profile == spec["swap_profile"],
                        Macro.event == spec["event"],
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                continue
            db.add(
                Macro(
                    name=spec["name"],
                    description=spec.get("description") or None,
                    printer_models=json.dumps(spec["printer_models"]),
                    swap_mode_only=True,
                    swap_profile=spec["swap_profile"],
                    event=spec["event"],
                    gcode=spec.get("gcode", ""),
                    is_custom=False,
                    enabled=True,
                )
            )
            seeded += 1

        if seeded:
            logger.info("m005: seeded %d empty swap-profile macro(s)", seeded)

        await db.commit()
