"""Old import path of the MakerWorld service — kept for migration m056 only.

MakerWorld lives in ``services/model_providers/makerworld/`` (upstream #2845).
The released migration m056 imports ``MakerWorldError`` and
``MakerWorldService`` from here, and migrations are frozen; new code imports
from the provider package (``tests/unit/test_old_makerworld_paths_serve_only_m056.py``).
"""

from backend.app.services.model_providers.makerworld.errors import MakerWorldError
from backend.app.services.model_providers.makerworld.service import MakerWorldService

__all__ = ["MakerWorldError", "MakerWorldService"]
