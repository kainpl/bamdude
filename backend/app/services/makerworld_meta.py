"""Old import path of the MakerWorld meta helpers — kept for migration m056 only.

The helpers live in ``services/model_providers/makerworld/meta.py`` (upstream
#2845). The released migration m056 imports ``build_meta_dict`` and
``download_covers`` from here, and migrations are frozen; new code imports
from the provider package (``tests/unit/test_old_makerworld_paths_serve_only_m056.py``).
"""

from backend.app.services.model_providers.makerworld.meta import build_meta_dict, download_covers

__all__ = ["build_meta_dict", "download_covers"]
