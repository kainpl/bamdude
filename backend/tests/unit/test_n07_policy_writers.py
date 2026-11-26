"""N07 spec §C.1, S20: every set-based write to ``oidc_providers`` either sets the
revision itself or touches no policy field — the mapper hook never sees those."""

from __future__ import annotations

import ast
from pathlib import Path

from backend.app.models.oidc_provider import POLICY_FIELDS

APP = Path(__file__).resolve().parents[2] / "app"
NOT_POLICY = {"is_autologin", "is_env_managed", "name", "icon_url", "icon_data", "icon_content_type", "icon_etag"}


def _set_based_writes():
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "values"):
                continue
            inner = node.func.value
            while isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                inner = inner.func.value
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "update"
                and inner.args
                and isinstance(inner.args[0], ast.Name)
                and inner.args[0].id == "OIDCProvider"
            ):
                yield path.relative_to(APP), node.lineno, {kw.arg for kw in node.keywords}


def test_every_set_based_provider_write_is_named_and_safe():
    writes = list(_set_based_writes())
    assert writes, "the scan found no set-based provider write at all"
    policy = set(POLICY_FIELDS)
    for path, line, columns in writes:
        assert None not in columns, f"{path}:{line} passes values as a mapping; name the columns"
        touches_policy = columns & policy
        assert columns <= NOT_POLICY | policy | {"policy_revision"}, f"{path}:{line} writes unknown columns {columns}"
        if touches_policy:
            assert "policy_revision" in columns, f"{path}:{line} changes {touches_policy} without a new revision"
