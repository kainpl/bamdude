"""Filesystem locations owned by products."""

from pathlib import Path

from backend.app.core.config import settings


def product_attachments_dir(product_id: int) -> Path:
    """``<archive_dir>/products/<id>/attachments`` — the product-side twin of
    ``routes/projects.py::get_project_attachments_dir``."""
    return Path(settings.archive_dir) / "products" / str(product_id) / "attachments"
