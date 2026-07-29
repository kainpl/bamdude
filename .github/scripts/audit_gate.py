#!/usr/bin/env python3
"""Fail CI on fixable high/critical npm-audit findings in production deps.

Was an inline heredoc in ci.yml until 2026-07-29. Moved out so it can be run
locally against the same inputs CI uses — a gate you cannot reproduce on your
machine is a gate people learn to ignore.

Three filters, in order:

1. **Production only.** ``npm audit --omit=dev`` still reports vulnerabilities in
   npm's own bundled dependencies, so the real prod set is read straight out of
   ``package-lock.json`` instead of trusting the audit's scoping.
2. **Fixable only.** A high-severity finding with no available fix is not
   actionable; failing on it would mean a permanently red gate that says nothing.
3. **Not accepted.** Findings listed in the exceptions file are reported but do
   not fail — see that file for what qualifies.

Exceptions are keyed by **GHSA advisory id, never by package name**. Muting a
package would hide the next, genuinely applicable hole in it.

Usage:
    python3 .github/scripts/audit_gate.py <audit.json> <package-lock.json> <exceptions.json>

Locally:
    cd frontend
    npm audit --omit=dev --json > /tmp/audit.json
    python3 ../.github/scripts/audit_gate.py /tmp/audit.json package-lock.json ../.github/audit-exceptions.json
"""

from __future__ import annotations

import json
import sys


def advisory_ids(name: str, vulns: dict, _seen: set[str] | None = None) -> set[str]:
    """Every GHSA id reachable from one vulnerability entry.

    ``via`` mixes two shapes: a dict is the advisory itself, a plain string is
    the name of another vulnerable package that drags this one in. The string
    case must be followed, not skipped — npm reports ``react-router-dom`` with
    no advisory of its own, only ``via: ["react-router"]``. Treating that as
    "unidentifiable" would make every transitively-affected package impossible
    to accept, and the exceptions file useless.

    ``_seen`` guards the cycle that mutually-dependent packages would create.
    """
    _seen = _seen if _seen is not None else set()
    if name in _seen:
        return set()
    _seen.add(name)

    ids: set[str] = set()
    for via in vulns.get(name, {}).get("via", []):
        if isinstance(via, dict):
            url = via.get("url") or ""
            if "/advisories/" in url:
                ids.add(url.rsplit("/", 1)[-1])
        elif isinstance(via, str):
            ids |= advisory_ids(via, vulns, _seen)
    return ids


def main() -> int:
    audit_path, lock_path, exceptions_path = sys.argv[1:4]

    with open(audit_path, encoding="utf-8") as fh:
        audit = json.load(fh)
    with open(lock_path, encoding="utf-8") as fh:
        lock = json.load(fh)
    with open(exceptions_path, encoding="utf-8") as fh:
        accepted = json.load(fh).get("exceptions", {})

    prod = {
        path.split("node_modules/")[-1]
        for path, info in lock.get("packages", {}).items()
        if path and not info.get("dev") and not info.get("devOptional")
    }

    vulns = audit.get("vulnerabilities", {})
    in_prod = {name: v for name, v in vulns.items() if name in prod}
    serious = {name: v for name, v in in_prod.items() if v.get("severity") in ("high", "critical")}

    blocking: dict[str, dict] = {}
    excused: list[tuple[str, str]] = []
    matched_ids: set[str] = set()

    for name, entry in serious.items():
        if not entry.get("fixAvailable"):
            continue
        ids = advisory_ids(name, vulns)
        hit = ids & accepted.keys()
        if hit:
            matched_ids |= hit
            excused.append((name, sorted(hit)[0]))
        else:
            blocking[name] = entry

    for name, ghsa in excused:
        note = accepted[ghsa]
        print(f"ACCEPTED {ghsa} ({name}) - review by {note.get('review_by', '?')}")
        print(f"         {note.get('why', '').split('.')[0]}.")

    # An exception that matches nothing means the dependency moved past it. Say so
    # loudly: a list nobody prunes turns into a blanket mute, which is the failure
    # mode this whole mechanism exists to avoid.
    for ghsa in accepted.keys() - matched_ids:
        print(f"STALE EXCEPTION {ghsa} matches no current finding - delete it from the exceptions file.")

    if blocking:
        for name, entry in blocking.items():
            ids = ", ".join(sorted(advisory_ids(name, vulns))) or "no advisory id"
            print(f"FIXABLE {entry['severity'].upper()}: {name} ({ids})")
        return 1

    print(
        f"npm audit: {len(serious)} high/critical in prod deps "
        f"({len(excused)} accepted, 0 fixable), {len(vulns)} total "
        f"({len(vulns) - len(in_prod)} npm-internal filtered)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
