"""scripts/ci/node_pin_report.py: what the weekly pin workflow outputs and writes into its one issue (spec D7)."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("node_pin_report", ROOT / "scripts" / "ci" / "node_pin_report.py")
report_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report_mod)

BEHIND = {
    "today": "2026-10-13",
    "action": True,
    "notice": False,
    "repin": True,
    "branch": "chore/node-pin-v24.21.0-v22.23.3",
    "lines": {
        "24": {
            "pinned": "v24.17.0",
            "newest": "v24.21.0",
            "behind": True,
            "missed_security": ["v24.18.1"],
            "end_of_life": "2028-04-30",
            "days_left": 565,
            "end_soon": False,
            "ended": False,
        },
        "22": {
            "pinned": "v22.23.3",
            "newest": "v22.23.3",
            "behind": False,
            "missed_security": [],
            "end_of_life": "2027-04-30",
            "days_left": 199,
            "end_soon": False,
            "ended": False,
        },
    },
    "new_lines": [],
}


def test_the_outputs_tell_the_workflow_what_to_do():
    assert report_mod.outputs(BEHIND).splitlines() == [
        "action=true",
        "notice=false",
        "repin=true",
        "branch=chore/node-pin-v24.21.0-v22.23.3",
        "report=" + json.dumps(BEHIND, separators=(",", ":"), ensure_ascii=False),
    ]


TESTED, MOVED, BASE = "a" * 40, "c" * 40, "b" * 40


def _body(report=BEHIND, *, repin="success", tested=TESTED, current=TESTED, smoke="success"):
    return report_mod.issue_body(
        report, repin=repin, tested_sha=tested, current_sha=current, base_sha=BASE, smoke=smoke, run_url="https://x/1"
    )


def test_the_issue_body_names_the_versions_and_the_missed_security_releases():
    body = _body()
    assert "v24.17.0 → v24.21.0" in body
    assert "v24.18.1" in body
    assert "https://x/1" in body


def test_a_failed_repin_has_nothing_to_merge():
    # consilium NL-M1: the signature, the golden or the push failed -- no candidate exists, so none is offered
    body = _body(repin="failure", tested="", current="", smoke="skipped")
    assert "nothing to merge" in body
    assert "Accept" not in body and "merge into" not in body


@pytest.mark.parametrize("smoke", ["failure", "cancelled", "skipped"])
def test_a_red_smoke_says_do_not_merge(smoke):
    body = _body(smoke=smoke)
    assert "do not merge" in body and smoke in body
    assert TESTED[:12] in body and "Accept" not in body


def test_a_green_candidate_that_is_still_the_head_is_offered():
    body = _body()
    assert TESTED[:12] in body and BASE[:12] in body
    assert "Accept it on the integrated revision" in body


def test_a_branch_that_moved_after_the_smoke_is_not_called_tested():
    # consilium NL2-R1: the smoke proved A; the branch is at B by the time of the report -- A stays the proof,
    # B is named untested, and nothing is offered for merging
    body = _body(current=MOVED)
    assert TESTED[:12] in body and MOVED[:12] in body
    assert "not tested" in body
    assert "Accept" not in body and "merge into" not in body


def test_a_branch_deleted_after_the_smoke_keeps_its_proof_and_offers_nothing():
    body = _body(current="")
    assert TESTED[:12] in body and "no longer exists" in body
    assert "Accept" not in body


def test_a_branch_advanced_with_real_git_never_borrows_the_old_success(tmp_path):
    # the reviewer's counterexample (consilium r2), kept: a plain fast-forward push after the smoke, no force
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q", "-b", "dev")
    git("config", "user.name", "t")
    git("config", "user.email", "t@example.invalid")
    git("config", "commit.gpgsign", "false")
    (tmp_path / "pin.txt").write_text("old pin")
    git("add", "pin.txt")
    git("commit", "-q", "-m", "base")
    git("checkout", "-q", "-b", BEHIND["branch"])
    (tmp_path / "pin.txt").write_text("candidate A")
    git("commit", "-q", "-am", "A")
    tested = git("rev-parse", "HEAD")  # what the repin job fixes and every smoke job checks out
    (tmp_path / "pin.txt").write_text("candidate B")
    git("commit", "-q", "-am", "B")
    current = git("ls-remote", ".", f"refs/heads/{BEHIND['branch']}").split()[0]
    base = git("merge-base", "dev", tested)
    body = report_mod.issue_body(
        BEHIND, repin="success", tested_sha=tested, current_sha=current, base_sha=base, smoke="success", run_url="u"
    )
    assert current != tested
    moved = next(line for line in body.splitlines() if current[:12] in line)
    assert "not tested" in moved and "passed" not in moved  # the line that names B never calls it proven
    assert "Accept" not in body


def test_an_ended_line_asks_the_maintainer_to_drop_it():
    ended_22 = {**BEHIND["lines"]["22"], "ended": True, "days_left": -3}
    ended = {**BEHIND, "repin": False, "lines": {**BEHIND["lines"], "22": ended_22}}
    body = _body(ended, repin="skipped", tested="", current="", smoke="skipped")
    assert "end of life" in body and "linux-armv7l" in body
    assert "merge" not in body


def test_a_new_lts_line_is_reported_not_pinned():
    newer = {**BEHIND, "repin": False, "new_lines": ["26"]}
    assert "Node.js 26" in _body(newer, repin="skipped", tested="", current="", smoke="skipped")


def test_an_accepted_candidate_says_it_repeats_until_the_release_line_reaches_dev():
    # final review M5: the check reads dev; a pin merged into a feature release line is still "behind" there
    assert "until that line reaches dev" in _body()


def test_the_candidate_names_the_base_it_came_from():
    # T12 on a throwaway branch: the issue said "from dev" for a candidate made from another base
    body = report_mod.issue_body(
        BEHIND,
        repin="success",
        tested_sha=TESTED,
        current_sha=TESTED,
        base_sha=BASE,
        base="feature/v0.7.0-fixes",
        smoke="success",
        run_url="https://x/1",
    )
    assert f"(from feature/v0.7.0-fixes `{BASE[:12]}`)" in body and "from dev" not in body


def test_a_scheduled_report_still_says_dev():
    assert f"(from dev `{BASE[:12]}`)" in _body()
