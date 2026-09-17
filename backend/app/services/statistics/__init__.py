"""Statistics: what the Stats page reads, computed on the server.

``aggregate`` - time series, breakdowns and records over ``print_archives``;
``failure_analysis`` - failure rates and correlations; ``energy`` - the two
ways a range total of energy is summed. Moved here from ``services/`` and
``routes/archives.py`` on 2026-09-17 (vault 60-specs/statistics-module-spec).

``reports/`` is RESERVED for the reports subsystem - not designed yet; the
route ``/statistics/reports`` is likewise an empty sub-router. Nothing goes
under either name without its own spec.
"""
