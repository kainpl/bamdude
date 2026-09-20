"""The password rules exist in two languages and must stay the same rules.

They already drifted twice, in opposite directions, and neither showed up as a
failing test:

* the frontend asked for 6 characters where the API wanted 8 plus a character
  mix, so a password the setup screen accepted came back as a 422;
* the API's own length floor sat in ``Field(min_length=...)`` on only two of
  the four schemas, so ``UserCreate`` took a three-character password the login
  form would then refuse to reproduce.

A test cannot import TypeScript, so it reads the source and looks for the
checks themselves. That is deliberately crude — it fails on a rewrite it should
not care about — but a crude guard that fires beats a subtle one nobody wrote.
The failure message points at this docstring, so a legitimate rewrite is a
one-line edit here rather than a mystery.
"""

import re
from pathlib import Path

import pytest

from backend.app.schemas import auth as auth_schemas

FRONTEND_RULES = Path(__file__).resolve().parents[3] / "frontend" / "src" / "utils" / "password.ts"

# The four checks, in the order the user meets them. Each side is asked for its
# own spelling of the same rule; the ORDER is part of the contract, because
# whichever rule is named first is the one the user fixes — a different order
# on each side means the message changes when the form is finally submitted.
FRONTEND_CHECKS = [
    "password.length >= MIN_PASSWORD_LENGTH",
    "/[A-Z]/.test(password)",
    "/[a-z]/.test(password)",
    "/\\d/.test(password)",
]
BACKEND_CHECKS = [
    "len(v) < MIN_PASSWORD_LENGTH",
    're.search(r"[A-Z]", v)',
    're.search(r"[a-z]", v)',
    're.search(r"\\d", v)',
]


@pytest.fixture(scope="module")
def frontend_source() -> str:
    if not FRONTEND_RULES.is_file():
        pytest.skip(f"frontend checkout not present at {FRONTEND_RULES}")
    return FRONTEND_RULES.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def backend_validator() -> str:
    source = Path(auth_schemas.__file__).read_text(encoding="utf-8")
    return source.split("def _validate_password_complexity")[1].split("\ndef ")[0]


def _positions(source: str, checks: list[str], where: str) -> list[int]:
    found = []
    for check in checks:
        index = source.find(check)
        assert index != -1, f"{where} no longer contains the check {check!r} — see this module's docstring"
        found.append(index)
    return found


def test_both_sides_agree_on_the_minimum_length(frontend_source):
    match = re.search(r"const MIN_PASSWORD_LENGTH = (\d+);", frontend_source)
    assert match, "frontend no longer declares MIN_PASSWORD_LENGTH"
    assert int(match.group(1)) == auth_schemas.MIN_PASSWORD_LENGTH


def test_both_sides_check_the_same_rules(frontend_source, backend_validator):
    _positions(frontend_source, FRONTEND_CHECKS, "frontend/src/utils/password.ts")
    _positions(backend_validator, BACKEND_CHECKS, "_validate_password_complexity")


def test_both_sides_check_them_in_the_same_order(frontend_source, backend_validator):
    front = _positions(frontend_source, FRONTEND_CHECKS, "frontend/src/utils/password.ts")
    back = _positions(backend_validator, BACKEND_CHECKS, "_validate_password_complexity")
    assert front == sorted(front), "the frontend reordered its rules"
    assert back == sorted(back), "the backend reordered its rules"


def test_neither_side_grew_a_special_character_rule(frontend_source, backend_validator):
    """Dropping that rule was a decision (NIST SP 800-63B advises against
    composition rules beyond length and a basic mix, and the friction pushed
    real operators towards worse-remembered passwords). Re-adding it has to be
    re-argued on both sides at once, not sneaked into one of them."""
    assert "!@#$" not in frontend_source
    assert "!@#$" not in backend_validator
