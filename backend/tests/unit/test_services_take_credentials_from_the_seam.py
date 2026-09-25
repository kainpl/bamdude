"""Services read Bambu Cloud credentials from the service seam, never from a route module.

``services/bambu_cloud_credentials.py`` is where the stored credential lives
(upstream #2845). A service importing it from ``api.routes.cloud`` couples the
service layer to a router module and its import graph — the shape the seam
exists to end.
"""

import ast
from pathlib import Path

SERVICES = Path(__file__).resolve().parents[2] / "app" / "services"
CLOUD_ROUTE = "backend.app.api.routes.cloud"
MOVED = {
    "get_stored_token",
    "get_stored_refresh_token",
    "is_cloud_token_invalid",
    "mark_cloud_token_invalid",
}


def _credential_imports_from_the_route(tree: ast.AST) -> list[str]:
    """Every way a module can reach the moved names through the cloud route:
    ``from …routes.cloud import get_stored_token``, ``from …routes import cloud``
    and ``import …routes.cloud`` (the last two then call ``cloud.get_stored_token``)."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == CLOUD_ROUTE:
                found.extend(f"{node.lineno} {name}" for name in sorted({a.name for a in node.names} & MOVED))
            elif node.module == "backend.app.api.routes" and any(a.name == "cloud" for a in node.names):
                found.append(f"{node.lineno} routes.cloud module")
        elif isinstance(node, ast.Import):
            found.extend(f"{node.lineno} {a.name}" for a in node.names if a.name == CLOUD_ROUTE)
    return found


def test_the_check_sees_every_import_form():
    source = (
        "from backend.app.api.routes.cloud import get_stored_token\n"
        "from backend.app.api.routes import cloud\n"
        "import backend.app.api.routes.cloud\n"
        "from backend.app.api.routes.cloud import build_authenticated_cloud\n"
    )

    assert _credential_imports_from_the_route(ast.parse(source)) == [
        "1 get_stored_token",
        "2 routes.cloud module",
        "3 backend.app.api.routes.cloud",
    ]


def test_no_service_imports_credentials_from_the_cloud_route():
    offenders = []
    for path in SERVICES.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders.extend(f"{path.relative_to(SERVICES)}:{hit}" for hit in _credential_imports_from_the_route(tree))
    assert not offenders, offenders
