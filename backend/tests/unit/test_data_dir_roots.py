"""Every subsystem owns a root under DATA_DIR; archive/ is the print history
and nothing else (vault 40-invariants/inv-data-dir-one-root-per-subsystem).

The autouse ``data_dir_isolation`` fixture points ``settings`` at a tmp
directory, so ``settings.data_dir`` here is that directory, not the live one.
"""

from backend.app.api.routes.library import get_library_dir, get_library_files_dir, get_library_thumbnails_dir
from backend.app.api.routes.projects import get_project_attachments_dir
from backend.app.api.routes.system import _classify_file, _get_data_dirs, _get_storage_rules
from backend.app.core.config import settings
from backend.app.services import backup_files
from backend.app.services.makerworld_meta import get_makerworld_covers_dir
from backend.app.services.product_files import product_attachments_dir


def test_library_projects_and_products_live_beside_archive_not_under_it():
    assert settings.library_dir == settings.data_dir / "library"
    assert settings.projects_dir == settings.data_dir / "projects"
    assert settings.products_dir == settings.data_dir / "products"
    assert get_library_dir() == settings.library_dir
    assert get_library_files_dir() == settings.library_dir / "files"
    assert get_library_thumbnails_dir() == settings.library_dir / "thumbnails"
    assert get_makerworld_covers_dir() == settings.library_dir / "makerworld-covers"
    assert get_project_attachments_dir(7) == settings.projects_dir / "7" / "attachments"
    assert product_attachments_dir(9) == settings.products_dir / "9" / "attachments"
    for path in (get_library_dir(), get_project_attachments_dir(7), product_attachments_dir(9)):
        assert settings.archive_dir not in path.parents, path


def test_the_backup_map_carries_every_root():
    dirs = backup_files.directories(settings)
    assert dirs["archive"] == settings.archive_dir
    assert dirs["library"] == settings.library_dir
    assert dirs["projects"] == settings.projects_dir
    assert dirs["products"] == settings.products_dir


def test_storage_usage_walks_the_new_roots_and_names_attachments():
    walked = _get_data_dirs()
    for root in (settings.library_dir, settings.projects_dir, settings.products_dir):
        assert root in walked, root
    rules = _get_storage_rules()
    assert _classify_file(settings.library_dir / "files" / "a.3mf", rules)[0] == "library_files"
    assert _classify_file(settings.library_dir / "thumbnails" / "a.png", rules)[0] == "library_thumbnails"
    assert _classify_file(settings.projects_dir / "1" / "attachments" / "x.jpg", rules)[0] == "attachments"
    assert _classify_file(settings.products_dir / "2" / "attachments" / "y.jpg", rules)[0] == "attachments"
    assert _classify_file(settings.archive_dir / "1" / "run" / "a.3mf", rules)[0] == "archive_files"
