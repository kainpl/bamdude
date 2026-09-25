"""Services read Bambu Cloud credentials from the service seam, never from a route module.

``services/bambu_cloud_credentials.py`` is where the stored credential lives
(upstream #2845). A service importing it from ``api.routes.cloud`` couples the
service layer to a router module and its import graph — the shape the seam
exists to end.
"""

import ast
from pathlib import Path

SERVICES = Path(__file__).resolve().parents[2] / "app" / "services"
MOVED = {
    "get_stored_token",
    "get_stored_refresh_token",
    "is_cloud_token_invalid",
    "mark_cloud_token_invalid",
}


def test_no_service_imports_credentials_from_the_cloud_route():
    offenders = []
    for path in SERVICES.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module == "backend.app.api.routes.cloud":
                names = {alias.name for alias in node.names} & MOVED
                if names:
                    offenders.append(f"{path.relative_to(SERVICES)}:{node.lineno} {sorted(names)}")
    assert not offenders, offenders
