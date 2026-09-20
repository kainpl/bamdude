"""Shape of the orders / products / customers tables (spec §Data model)."""

from sqlalchemy import inspect

from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.customer import Customer
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.product import Product, ProductPart, ProductPlate, product_files, product_folders
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine, ProjectProcurement


def _cols(model) -> set[str]:
    return {c.name for c in inspect(model).columns}


def _unique_names(model) -> set[str]:
    return {c.name for c in model.__table__.constraints if c.name}


def test_project_is_an_order_now():
    cols = _cols(Project)
    for gone in ("target_count", "target_parts_count", "parent_id", "is_template", "template_source_id", "budget"):
        assert gone not in cols
    for present in (
        "customer_id",
        "price",
        "status",
        "color",
        "url",
        "tags",
        "notes",
        "attachments",
        "cover_image_filename",
    ):
        assert present in cols


def test_product_tables_and_uniques():
    assert {
        "kind",
        "name",
        "name_key",
        "qty_per_unit",
        "aliases",
        "auto",
        "unit_price",
        "sourcing_url",
        "remarks",
    } <= _cols(ProductPart)
    assert "uq_product_parts_key" in _unique_names(ProductPart)
    assert {"product_id", "library_file_id", "plate_index"} <= _cols(ProductPlate)
    assert "uq_product_plates_file_plate" in _unique_names(ProductPlate)
    assert {
        "is_active",
        "cover_image_filename",
        "attachments",
        "designer",
        "license",
        "source_url",
        "design_id",
    } <= _cols(Product)
    assert {c.name for c in product_files.columns} == {"product_id", "library_file_id"}
    assert {c.name for c in product_folders.columns} == {"product_id", "library_folder_id"}


def test_lines_procurement_and_customers():
    assert {"project_id", "product_id", "quantity", "material", "color", "note", "sort_order"} <= _cols(ProjectLine)
    assert {"project_id", "product_part_id", "quantity_acquired"} == _cols(ProjectProcurement)
    assert {"name", "contact", "notes"} <= _cols(Customer)


def test_project_line_id_travels_queue_to_archive():
    for model in (PrintArchive, PrintQueueItem, AutoQueueItem):
        assert "project_line_id" in _cols(model)


def test_library_links_target_products():
    assert "products" in inspect(LibraryFile).relationships
    assert "products" in inspect(LibraryFolder).relationships
    assert "projects" not in inspect(LibraryFile).relationships
    assert "projects" not in inspect(LibraryFolder).relationships
