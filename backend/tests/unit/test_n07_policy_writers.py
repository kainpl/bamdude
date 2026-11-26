"""N07 spec §C.1, S20: every set-based write to ``oidc_providers`` either sets the
revision itself or touches no policy field — the mapper hook never sees those."""

from __future__ import annotations

import ast
from pathlib import Path

from backend.app.models.oidc_provider import POLICY_FIELDS

APP = Path(__file__).resolve().parents[2] / "app"
NOT_POLICY = {"is_autologin", "is_env_managed", "name", "icon_url", "icon_data", "icon_content_type", "icon_etag"}


def _is_provider(node) -> bool:
    """``OIDCProvider`` or ``OIDCProvider.__table__``."""
    if isinstance(node, ast.Name):
        return node.id == "OIDCProvider"
    return isinstance(node, ast.Attribute) and node.attr == "__table__" and _is_provider(node.value)


def _scan(tree):
    """Every ``update(OIDCProvider…)``: the columns its ``.values()`` names, or ``None`` when it is
    executed without ``.values()`` (the executemany form with a parameter list, N07-A11)."""
    found = []
    with_values = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "values":
            inner = node.func.value
            while isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                inner = inner.func.value
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "update"
                and inner.args
                and _is_provider(inner.args[0])
            ):
                with_values.add(id(inner))
                columns = {kw.arg for kw in node.keywords}
                if node.args:  # a positional dict or list: its columns are unknown to the scan (N07-A12)
                    columns.add(None)
                found.append((node.lineno, columns))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "update"
            and node.args
            and _is_provider(node.args[0])
            and id(node) not in with_values
        ):
            found.append((node.lineno, None))
    return found


def _set_based_writes():
    for path in APP.rglob("*.py"):
        for line, columns in _scan(ast.parse(path.read_text(encoding="utf-8"))):
            yield path.relative_to(APP), line, columns


def _verdicts(source: str) -> list[str]:
    policy = set(POLICY_FIELDS)
    out = []
    for line, columns in _scan(ast.parse(source)):
        if columns is None:
            out.append(f"{line}: executed without values()")
        elif None in columns:
            out.append(f"{line}: values from a mapping")
        elif columns & policy and "policy_revision" not in columns:
            out.append(f"{line}: policy without a revision")
    return out


def test_the_scan_reads_every_set_based_form():
    """The guard's own fixtures (N07-A11): the forms it must catch, and the ones it must pass."""
    caught = {
        "table form": "update(OIDCProvider.__table__).values(is_enabled=False)",
        "executemany": "session.execute(update(OIDCProvider), [{'id': 1, 'is_enabled': False}])",
        "mapping values": "update(OIDCProvider).values(**changes)",
        "positional dict": "update(OIDCProvider).values({'is_enabled': False})",
        "chained where": "update(OIDCProvider).where(OIDCProvider.id == 1).values(client_id='x')",
    }
    for name, source in caught.items():
        assert _verdicts(source), name
    passed = (
        "update(OIDCProvider).where(OIDCProvider.id != 1).values(is_autologin=False)",
        "update(OIDCProvider).values(is_enabled=False, policy_revision='r')",
    )
    for source in passed:
        assert _verdicts(source) == [], source


def test_no_provider_write_bypasses_both_the_hook_and_the_scan():
    """Raw SQL, ``OIDCProvider.__table__`` updates and bulk ORM updates would skip the mapper
    hook and this scan alike; outside the frozen migrations there must be none."""
    import re

    patterns = (
        r"(?i)update\s+oidc_providers",
        r"OIDCProvider\.__table__\.update",
        r"bulk_update_mappings\(\s*OIDCProvider",
    )
    offenders = []
    for path in APP.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            offenders += [f"{path.relative_to(APP)}: {m.group(0)}" for m in re.finditer(pattern, text)]
    assert offenders == []


def test_every_callback_refusal_rolls_back_first():
    """N07-A05: the callback's only RedirectResponse outside its refusal helper is the success."""
    tree = ast.parse((APP / "api/routes/mfa.py").read_text(encoding="utf-8"))
    callback = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "oidc_callback")
    helper = next(n for n in ast.walk(callback) if isinstance(n, ast.AsyncFunctionDef) and n.name == "refusal")
    body = [n for n in helper.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    assert ast.unparse(body[0]) == "await db.rollback()"
    inside_helper = {id(n) for n in ast.walk(helper)}
    redirects = [
        ast.unparse(n)
        for n in ast.walk(callback)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "RedirectResponse" and id(n) not in inside_helper
    ]
    assert len(redirects) == 1 and "#oidc_token=" in redirects[0], redirects


def test_every_set_based_provider_write_is_named_and_safe():
    writes = list(_set_based_writes())
    assert writes, "the scan found no set-based provider write at all"
    policy = set(POLICY_FIELDS)
    for path, line, columns in writes:
        assert columns is not None, f"{path}:{line} executes update(OIDCProvider) without values()"
        assert None not in columns, f"{path}:{line} passes values as a mapping; name the columns"
        touches_policy = columns & policy
        assert columns <= NOT_POLICY | policy | {"policy_revision"}, f"{path}:{line} writes unknown columns {columns}"
        if touches_policy:
            assert "policy_revision" in columns, f"{path}:{line} changes {touches_policy} without a new revision"
