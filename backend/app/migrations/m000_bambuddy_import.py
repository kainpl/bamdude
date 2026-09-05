"""Version-0 record — once the Bambuddy 2.2.2 import, a stub since 0.5.6.

This migration used to import an upstream Bambuddy 2.2.2 database found in the
data directory into a fresh BamDude install. Removed on 2026-09-05: BamDude
forked at Bambuddy 2.2.2 and the two schemas have diverged far past the point
where a one-time import is safe, so the importer is gone and a Bambuddy file is
now left strictly alone.

The module stays because version 0 is a real link in the chain: ``_run_pending``
discovers it and ``_bootstrap_existing`` records version 0 under this exact name
for installs that predate the migration system. Deleting the module would make
those recorded rows reference a migration that no longer exists.

All that is left is the warning below, so an operator who dropped a Bambuddy
database in expecting an import is told why nothing happened. It repeats on
every start by design — nothing renames or removes the file any more, so the
operator deletes it themselves once they no longer need it.

Note that our OWN legacy filename handling is untouched and lives in
``migrations/__init__.py``: a ``bambuddy.db`` written by BamDude 3.0.1 is still
renamed to ``bamdude.db`` and upgraded, and that rename runs BEFORE this stub.
"""

import logging

version = 0
name = "bambuddy_to_bamdude_301"

logger = logging.getLogger(__name__)


async def seed(session_factory):
    """Warn about a genuine Bambuddy database in the data directory; import nothing."""
    from backend.app.core.config import settings
    from backend.app.migrations import _find_legacy_database, _is_bamdude_301

    legacy_path = _find_legacy_database(settings.data_dir)
    if not legacy_path or await _is_bamdude_301(legacy_path):
        # Either nothing legacy is there, or it is our own 3.0.1 file, which
        # __init__.py has already renamed and upgraded.
        return

    logger.warning(
        "Found a Bambuddy database at %s. Importing Bambuddy data was removed in 0.5.6 — "
        "BamDude and Bambuddy have diverged too far for a one-time import; the file is left "
        "untouched. See https://docs.bamdude.top/getting-started/upgrading/",
        legacy_path,
    )
