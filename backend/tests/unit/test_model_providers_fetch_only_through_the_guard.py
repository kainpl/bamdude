"""A configured-origin provider has no HTTP client of its own.

Its address comes from a settings form, so the rules about where a request may go live in ONE
transport (``services/model_providers/outbound.py``). A provider module that imported an HTTP
library could open a connection the transport never saw — so none may.

MakerWorld is the named exception: its destinations are constants of the code (the static host
lists of its descriptor), enforced by its own service and tests.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROVIDERS = Path(__file__).resolve().parents[2] / "app" / "services" / "model_providers"

#: Packages whose destinations are constants of the code, not a configured address.
STATIC_HOST_PROVIDERS = {"makerworld"}
#: Modules of the package itself that are not a provider.
SHARED = {"outbound.py", "base.py", "registry.py", "__init__.py"}

NETWORK_MODULES = {"httpx", "aiohttp", "requests", "urllib3", "socket", "ssl", "http.client", "urllib.request"}


def _network_imports(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
        found += [name for name in names if any(name == m or name.startswith(m + ".") for m in NETWORK_MODULES)]
    return found


def _configured_origin_modules() -> list[Path]:
    modules = []
    for entry in sorted(PROVIDERS.iterdir()):
        if entry.is_dir() and entry.name not in STATIC_HOST_PROVIDERS and entry.name != "__pycache__":
            modules += sorted(entry.rglob("*.py"))
        elif entry.is_file() and entry.suffix == ".py" and entry.name not in SHARED:
            modules.append(entry)
    return modules


def test_manyfold_is_a_configured_origin_provider():
    assert (PROVIDERS / "manyfold" / "service.py").is_file()
    assert any(path.parent.name == "manyfold" for path in _configured_origin_modules())


def test_no_configured_origin_provider_imports_an_http_library():
    offenders = {
        str(path.relative_to(PROVIDERS)): imports
        for path in _configured_origin_modules()
        if (imports := _network_imports(path))
    }
    assert offenders == {}, (
        f"{offenders}: a configured-origin provider sends every request through outbound.GuardedTransport"
    )


def test_the_guard_sees_an_import():
    """The scan itself: it must find the transport's own import, or it proves nothing."""
    assert "httpx" in _network_imports(PROVIDERS / "outbound.py")
