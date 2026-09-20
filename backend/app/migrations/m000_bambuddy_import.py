"""Version-0 record — once the Bambuddy 2.2.2 import, inert since 0.5.6.

This migration used to import an upstream Bambuddy 2.2.2 database found in the
data directory into a fresh BamDude install. Removed on 2026-09-05: BamDude
forked at Bambuddy 2.2.2 and the two schemas have diverged far past the point
where a one-time import is safe, so the importer is gone and a Bambuddy file is
now left strictly alone.

The module stays because version 0 is a real link in the chain: ``_run_pending``
discovers it and ``_bootstrap_existing`` records version 0 under this exact name
for installs that predate the migration system. Deleting the module would leave
those recorded rows pointing at a migration that no longer exists.

It does nothing at all, and that is the point. The notice about a Bambuddy file
found in the data directory is **not** here: a migration is recorded in
``_migrations`` and runs exactly once, so a notice in this module would fire on
one boot and never again, while the file it describes stays where it is. It
lives in ``migrations/__init__.py::_warn_if_foreign_bambuddy_file``, which runs
on every start.

Our OWN legacy filename handling is untouched and also lives in ``__init__.py``:
a ``bambuddy.db`` written by BamDude 3.0.1 is still renamed to ``bamdude.db``
and upgraded, before any of this runs.
"""

version = 0
name = "bambuddy_to_bamdude_301"


async def seed(session_factory):
    """Do nothing. See the module docstring: kept only for the version-0 record."""
    return
