"""Telling the printer's own internal jobs apart from a user's print.

Bambu firmware runs jobs on its own behalf — bed levelling, vibration
compensation, the pressure-advance line it lays down before a print when flow
dynamics calibration is on — and reports them over MQTT through exactly the same
print-start and print-complete events a real print uses. Nothing in the event
says "this one is mine": the job has to be recognised by name (upstream
9c938843, a4a1f4c5, 164382b3).

Getting it wrong is not free. An unrecognised calibration run has no 3MF
anywhere on the printer, so the archive path sweeps for a file that cannot
exist and writes a no-3MF archive named after the calibration; and its
completion takes the no-archive notification path, which attributes an
unmatched completion to a queue item finished in the last five minutes and
emails that print's owner.

A leaf module with no imports of its own, so the print-start, adoption and
print-complete paths share one answer.
"""

# Job names the printer runs for itself. Matched EXACTLY (after normalising), not
# by prefix or substring: "auto", "calib" and "pa_" are ordinary words in a user's
# own filenames — and BamDude's own calibration wizard dispatches
# ``auto_pa_line_single.3mf`` / ``auto_pa_line_dual.3mf``, which ARE prints.
#
# ``auto_cali_for_user`` — the bed-levelling / vibration run, normally reported
#   with a ``/usr/etc/print/`` path the rule below catches on its own; listed
#   anyway because the path is not guaranteed.
# ``auto_pa_line_calib_mode`` — the pressure-advance (K profile) line run before a
#   print; reported as a SUBTASK name with no ``/usr/`` path at all.
# ``pa_line_calib_mode`` / ``pa_pattern_calib_mode`` — the same calibration
#   started by hand (a line or a pattern), under their own names with no
#   ``auto_`` prefix (BambuStudio ``CalibUtils.cpp``).
INTERNAL_JOB_NAMES = frozenset(
    {
        "auto_cali_for_user",
        "auto_pa_line_calib_mode",
        "pa_line_calib_mode",
        "pa_pattern_calib_mode",
    }
)

# Longest first: ``.gcode.3mf`` has to be stripped whole, or ``.3mf`` would match
# first and leave a trailing ``.gcode`` behind.
_PRINT_SUFFIXES = (".gcode.3mf", ".gcode", ".3mf")


def _normalize_job_name(value: str) -> str:
    """A reported name reduced to something comparable against the set above.

    Drops any directory part, one print-file suffix, and case — the printer is
    not consistent about which of these it includes.
    """
    name = value.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].strip().casefold()
    for suffix in _PRINT_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def is_internal_printer_job(filename: str | None, subtask_name: str | None = None) -> bool:
    """True when this print event belongs to the printer, not to a user.

    Both fields are tested because neither is reliably populated: the
    pressure-advance line arrives as a subtask name with no filename, the
    levelling run as a ``/usr/etc/print/...`` path. Internal if EITHER says so.
    """
    if filename and filename.startswith("/usr/"):
        # Bambu keeps its own calibration gcode on the read-only system
        # partition; nothing a user prints lives there.
        return True
    return any(_normalize_job_name(value) in INTERNAL_JOB_NAMES for value in (filename, subtask_name) if value)
