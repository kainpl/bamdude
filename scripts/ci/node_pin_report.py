"""The weekly Node pin workflow's words (part-render-node-lines-spec D7): its job outputs and its one issue.

python scripts/ci/node_pin_report.py outputs check.json >> "$GITHUB_OUTPUT"
python scripts/ci/node_pin_report.py issue check.json --smoke <result> --run-url <url> > body.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# the platforms of a line, for the "drop it" sentence -- the pin script's table, read without importing gpg paths
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pin_render_runtime import LINES  # noqa: E402


def outputs(report: dict) -> str:
    return "\n".join(
        [
            f"action={str(report['action']).lower()}",
            f"notice={str(report['notice']).lower()}",
            f"repin={str(report['repin']).lower()}",
            f"branch={report['branch']}",
            # the whole report as an immutable job output: the job that ran third-party packages holds the run's
            # artifact token and could replace an artifact, never another job's outputs (security review)
            "report=" + json.dumps(report, separators=(",", ":"), ensure_ascii=False),
        ]
    )


def issue_body(
    report: dict,
    *,
    repin: str,
    tested_sha: str,
    current_sha: str,
    base_sha: str,
    smoke: str,
    run_url: str,
    base: str = "dev",
) -> str:
    """What the weekly run found and what really exists. A smoke result belongs to the ONE commit the repin job fixed
    and every smoke job checked out (``tested_sha``), never to whatever the branch points at by report time
    (``current_sha``): a candidate is offered only when it is green AND still the branch head (consilium NL-M1,
    NL2-R1). ``base`` is the branch the run read: dev on the schedule, another on a manual run (T12)."""
    lines = ["The weekly check of the pinned Node.js (spec part-render-node-lines) found work.", ""]
    for line, entry in sorted(report["lines"].items(), key=lambda item: -int(item[0])):
        state = f"{entry['pinned']} → {entry['newest']}" if entry["behind"] else f"{entry['pinned']} (current)"
        lines.append(f"- **Node.js {line}:** {state}; end of life {entry['end_of_life']} ({entry['days_left']} days)")
        if entry["missed_security"]:
            lines.append(f"  - security releases not pinned yet: {', '.join(entry['missed_security'])}")
        if entry["ended"]:
            lines.append(
                f"  - **reached its end of life:** drop {', '.join(LINES.get(line, ()))} from LINES and "
                "render_runtime.PLATFORMS, then re-pin (spec D3)"
            )
        elif entry["end_soon"]:
            lines.append("  - ends within 90 days: plan the release that drops its platforms (spec D3)")
    for line in report["new_lines"]:
        lines.append(
            f"- **Node.js {line}** is an LTS line now: a maintainer decision with acceptance, never pinned here"
        )
    if report["repin"]:
        branch = report["branch"]
        lines.append("")
        if not tested_sha:
            lines.append(f"**Re-pin failed — no candidate on `{branch}`, nothing to merge** (re-pin job: {repin}).")
        else:
            lines.append(f"Candidate `{tested_sha[:12]}` on `{branch}` (from {base} `{base_sha[:12] or 'unknown'}`).")
            if smoke != "success":
                lines.append(f"**Smoke: {smoke} — do not merge.**")
            else:
                lines.append(f"The smoke passed on every platform for `{tested_sha[:12]}`.")
            if not current_sha:
                lines.append(f"**The branch `{branch}` no longer exists — nothing to merge from it.**")
            elif current_sha != tested_sha:
                lines.append(
                    f"**The branch has moved to `{current_sha[:12]}` since; that commit is not tested — run the smoke "
                    "on it before merging.**"
                )
            elif smoke == "success":
                lines.append(
                    "Accept it on the integrated revision: merge it into the release line -- the branch is named by "
                    "its versions and may be older than dev -- and run the release manual's macOS acceptance there."
                )
                # final review M5: the check reads dev, so a pin merged into a feature release line is behind there
                lines.append("Once merged into the release line, this issue repeats until that line reaches dev.")
    lines += ["", f"Run: {run_url}"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("outputs", "issue"):
        p = sub.add_parser(name)
        p.add_argument("report", type=Path)
        if name == "issue":
            for flag in ("--repin", "--smoke"):
                p.add_argument(flag, default="skipped")
            for flag in ("--tested-sha", "--current-sha", "--base-sha", "--run-url"):
                p.add_argument(flag, default="")
            p.add_argument("--base", default="dev")
    args = parser.parse_args(argv)
    report = json.loads(args.report.read_text(encoding="utf-8"))
    if args.cmd == "outputs":
        print(outputs(report))
        return 0
    body = issue_body(
        report,
        repin=args.repin,
        tested_sha=args.tested_sha,
        current_sha=args.current_sha,
        base_sha=args.base_sha,
        base=args.base,
        smoke=args.smoke,
        run_url=args.run_url,
    )
    print(body, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
