"""Schema-level tests for ``validate_http_url`` (audit B.2 / #1155).

The validator is the only XSS gate between the user's free-text input
and a clickable ``<a href={...}>`` in the React tree, so its contract has
to be airtight: anything that isn't ``http(s)://`` must raise — including
the silent attack vectors (``javascript:``, ``data:``, ``file:``,
``vbscript:``, etc.) that React doesn't escape inside ``href``.

It started life as ``_validate_project_url``, guarding one column. The
projects redesign made it public and gave it four more callers —
``Project.url``, ``Product.source_url`` and ``ProductPart.sourcing_url``,
each on both the create and the update path — so the propagation cases
below cover all three entry points, not just the order's.
"""

import pytest
from pydantic import ValidationError

from backend.app.schemas.product import ProductCreate, ProductPartCreate
from backend.app.schemas.project import ProjectCreate, ProjectUpdate, validate_http_url


@pytest.mark.parametrize(
    "good",
    [
        "http://example.com",
        "https://example.com/path?q=1",
        "HTTP://CAPS.example",
        "  https://trim.example/  ",  # leading/trailing whitespace stripped
    ],
)
def test_valid_http_urls_pass_through(good):
    assert validate_http_url(good) is not None


def test_whitespace_only_collapses_to_none():
    assert validate_http_url("   ") is None


def test_none_passes_through_as_none():
    assert validate_http_url(None) is None


@pytest.mark.parametrize(
    "bad",
    [
        "javascript:alert(1)",
        "JAVASCRIPT:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///etc/passwd",
        "vbscript:msgbox(1)",
        "ftp://files.example",
        "//example.com",  # protocol-relative — browsers resolve to current scheme
        "example.com",
        "/relative/path",
    ],
)
def test_non_http_schemes_rejected(bad):
    with pytest.raises(ValueError):
        validate_http_url(bad)


def test_project_create_propagates_validator_failure():
    with pytest.raises(ValidationError):
        ProjectCreate.model_validate({"name": "x", "url": "javascript:alert(1)"})


def test_project_create_accepts_valid_url():
    proj = ProjectCreate.model_validate({"name": "x", "url": "https://example.com"})
    assert proj.url == "https://example.com"


def test_project_update_validator_runs_on_url_field():
    with pytest.raises(ValidationError):
        ProjectUpdate.model_validate({"url": "data:text/html,evil"})


def test_project_update_clears_via_explicit_null():
    """Setting ``url`` to None on update must round-trip cleanly so the
    UI can clear a previously set link without dropping unrelated
    fields."""
    upd = ProjectUpdate.model_validate({"url": None})
    assert upd.url is None
    assert "url" in upd.model_fields_set


def test_product_source_url_propagates_validator_failure():
    with pytest.raises(ValidationError):
        ProductCreate.model_validate({"name": "Lamp", "source_url": "javascript:alert(1)"})
    product = ProductCreate.model_validate({"name": "Lamp", "source_url": "  https://makerworld.com/m/1  "})
    assert product.source_url == "https://makerworld.com/m/1"


def test_product_part_sourcing_url_propagates_validator_failure():
    with pytest.raises(ValidationError):
        ProductPartCreate.model_validate({"kind": "purchased", "name": "M3", "sourcing_url": "file:///etc/passwd"})
    part = ProductPartCreate.model_validate({"kind": "purchased", "name": "M3", "sourcing_url": "https://shop/m3"})
    assert part.sourcing_url == "https://shop/m3"
