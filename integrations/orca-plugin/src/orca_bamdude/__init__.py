"""OrcaBamDude -- an OrcaSlicer plugin that sends sliced plates to a BamDude farm over its HTTP API.

The modules here are host-independent and import only the standard library, so they run and are tested outside
OrcaSlicer. The host adapter (printer agent, tab, camera relay wiring) imports ``orca`` and is loaded only by the
host.
"""

__version__ = "0.1.0"
