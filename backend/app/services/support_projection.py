"""Fit the bug-report pack inside a GitHub issue, and say what did not fit.

The downloaded ZIP has no size limit and takes the whole payload. The issue
pack does: the relay pretty-prints ``support_info`` into an issue body, and
GitHub caps a body at 65 536 characters. Exceeding it is not a trimmed tail —
the API answers 422, the report is lost along with the description the reporter
typed, and what they are shown is "Failed to create GitHub issue", which reads
as the relay being down rather than as their own farm being large.

Measured on a 10-printer farm (2026-08-17): 22 781 characters of support info,
29 368 of logs. Two sections scale with the farm — ``printers`` and
``diagnostics``, together **1 350 characters per printer** — against a fixed
base of 9 279. Solving for the limit: a report fails above **19 printers** with
a short description, and above **13** with the 8 000 characters the relay
allows. That is a present-tense failure on a farm one size up from the one this
was measured on, not a precaution.

Pure and synchronous on purpose: a dict in, a dict out, so the rules here are
tested without a database, a printer or a relay.
"""

from __future__ import annotations

import json

# GitHub's hard cap on an issue body.
ISSUE_BODY_LIMIT = 65_536

# What is left for the pack once the body's other tenants are paid for: 8 000
# the relay allows a description, ~1 000 of boilerplate, screenshot link and
# email block. That leaves 56 536; rounded DOWN to 56 000, and the margin
# absorbs the JSON fences, the <details> wrappers and the markers written below
# — none of which are counted in the sections themselves.
ISSUE_PACK_BUDGET = 56_000

# Kept in this order while the budget lasts.
#
# Everything before ``printers`` is under 1 KB and ~3.9 KB together — the
# cheapest diagnosis per byte in the payload, so it is never what has to go.
# ``recent_logs`` is last because it is the one section that can spend whatever
# remains without losing a discrete fact: a log is still a log when it is
# shorter.
_LADDER: tuple[str, ...] = (
    "generated_at",
    "app",
    "system",
    "environment",
    "network",
    "dependencies",
    "process",
    "docker",
    "database",
    "database_health",
    "auth",
    "integrations",
    "virtual_printers",
    "websockets",
    "log_file",
    "library",
    "inventory",
    "maintenance",
    "printers",
    "queue",
    "settings",
    "diagnostics",
    "recent_logs",
)

# Quotes, colon, comma and indentation around a key in a pretty-printed object.
_KEY_OVERHEAD = 8


def _size(value) -> int:
    return len(json.dumps(value, indent=2, default=str))


def project_for_issue(info: dict, budget_chars: int = ISSUE_PACK_BUDGET) -> tuple[dict, list[str]]:
    """Return a copy of ``info`` that fits ``budget_chars``, plus what was cut.

    Sections are kept whole, in :data:`_LADDER` order, until the budget runs
    out. A section that does not fit is REPLACED by a marker rather than
    removed: an absent key reads as "this install had none of this", which is a
    different and wrong diagnosis. ``recent_logs`` is trimmed line by line from
    the front, keeping the newest — a report is filed just after reproducing the
    fault, so the error is at the end.

    Never mutates ``info``: the caller still owns the full payload, and the ZIP
    is built from it.
    """
    notes: list[str] = []
    out: dict = {}
    spent = 2  # the enclosing braces of the JSON object

    ordered = [k for k in _LADDER if k in info] + [k for k in info if k not in _LADDER]

    for key in ordered:
        value = info[key]
        cost = _size(value) + len(key) + _KEY_OVERHEAD
        room = budget_chars - spent - len(key) - _KEY_OVERHEAD

        if spent + cost <= budget_chars:
            out[key] = value
            spent += cost
            continue

        if key == "recent_logs" and isinstance(value, str):
            kept, note = _trim_logs(value, room)
            out[key] = kept or "omitted: no room left in the issue budget"
            spent += len(out[key]) + len(key) + _KEY_OVERHEAD
            notes.append(note)
            continue

        # ⚠️ A list is trimmed, never dropped. ``printers`` and the per-printer
        # diagnostics are the sections that grow with the farm — so keeping
        # them whole would discard them entirely at exactly the size where they
        # matter most, which is the failure this whole module exists to fix.
        # Some printers described beats none.
        if isinstance(value, list) and value:
            kept_items, note = _trim_list(key, value, room)
            if kept_items:
                out[key] = kept_items
                spent += _size(kept_items) + len(key) + _KEY_OVERHEAD
                notes.append(note)
                continue

        marker = f"omitted: {_size(value):,} chars, over the issue budget — see the support bundle ZIP"
        out[key] = marker
        spent += len(marker) + len(key) + _KEY_OVERHEAD
        notes.append(f"{key}: omitted ({_size(value):,} chars over budget)")

    # The accounting above is an estimate — per-key overhead in a pretty-printed
    # object depends on nesting depth. This is the guarantee: measure what was
    # actually built and, while it is still too big, replace whatever is largest
    # with a marker. Deterministic, and it converges because every replacement
    # is strictly smaller than what it replaces.
    trimmed_further: set[str] = set()
    while _size(out) > budget_chars:
        largest = max(
            (k for k in out if not _is_marker(out[k])),
            key=lambda k: _size(out[k]),
            default=None,
        )
        if largest is None:
            return {"_truncated": "the whole pack was over the issue budget — see the support bundle ZIP"}, [
                *notes,
                "everything: omitted, the budget was too small for any section",
            ]

        value = out[largest]
        # Shrink before discarding. Replacing a trimmed list wholesale would
        # undo the work above and hand back nothing for the section that most
        # needed to survive.
        if isinstance(value, list) and value:
            value.pop()
            trimmed_further.add(largest)
            continue
        if isinstance(value, str) and "\n" in value:
            out[largest] = "\n".join(value.splitlines()[len(value.splitlines()) // 2 :])
            trimmed_further.add(largest)
            continue

        notes.append(f"{largest}: omitted ({_size(value):,} chars over budget)")
        out[largest] = "omitted: over the issue budget — see the support bundle ZIP"

    for key in sorted(trimmed_further):
        notes.append(f"{key}: shortened further to fit the measured size of the pack")

    return out, notes


def _is_marker(value) -> bool:
    return isinstance(value, str) and value.startswith("omitted:")


def _trim_list(key: str, items: list, room: int) -> tuple[list, str]:
    """Keep as many leading entries as fit, and say how many were left out."""
    kept: list = []
    for item in items:
        candidate = [*kept, item]
        if _size(candidate) > room:
            break
        kept = candidate
    dropped = len(items) - len(kept)
    return kept, f"{key}: kept {len(kept)} of {len(items)} entries to fit the issue budget ({dropped} dropped)"


def _trim_logs(logs: str, room: int) -> tuple[str, str]:
    """Keep the newest whole lines that fit in ``room``."""
    lines = logs.splitlines()
    if room <= 0:
        return "", f"logs: omitted entirely, no room left (had {len(lines)} lines)"

    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        if used + len(line) + 1 > room:
            break
        kept.append(line)
        used += len(line) + 1
    kept.reverse()
    return "\n".join(kept), f"logs: kept the last {len(kept)} of {len(lines)} lines to fit the issue budget"
