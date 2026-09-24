"""A Bearer token is told from a JWT by ``core/auth.is_api_key_token`` — nowhere else.

Seven places used to spell ``startswith("bb_")`` each on their own. When new
keys moved to ``bd_`` (Bambuddy's ``bb_`` stays accepted), any copy left behind
would have turned every new key into a 401 on that one route — the middleware,
a gate, ``/auth/me`` or the cloud owner lookup. This walks ``backend/app`` and
fails on a ``startswith`` whose argument is a literal key prefix.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
_PREFIX_LITERALS = ("bb_", "bd_", "Bearer bb_", "Bearer bd_")


def _literal_prefix_checks() -> list[str]:
    found = []
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "startswith" or not node.args:
                continue
            arg = node.args[0]
            literals = arg.elts if isinstance(arg, ast.Tuple) else [arg]
            for literal in literals:
                if isinstance(literal, ast.Constant) and literal.value in _PREFIX_LITERALS:
                    found.append(f"{path.relative_to(APP)}:{node.lineno}")
    return found


def test_no_route_spells_the_key_prefix_itself():
    assert not _literal_prefix_checks(), _literal_prefix_checks()


def test_both_prefixes_are_keys_and_a_jwt_is_not():
    from backend.app.core.auth import generate_api_key, is_api_key_token

    new_key = generate_api_key()[0]

    assert new_key.startswith("bd_")
    assert is_api_key_token(new_key)
    assert is_api_key_token("bb_" + "x" * 43)
    assert not is_api_key_token("eyJhbGciOiJIUzI1NiJ9.e30.sig")
    assert not is_api_key_token("")
    assert not is_api_key_token(None)
