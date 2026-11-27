# STEP fixture generators

These wrote the STEP test fixtures beside this folder; the tests read the committed
files, so run a generator only to change or rebuild them.

- `make_fixtures.py <out_dir>` writes `authored/`: planar solids and assemblies through
  `p21.py`, a minimal ISO 10303-21 writer with no OCCT, plus `expected.json` computed from
  the construction parameters. The output is deterministic: a rebuild is byte-identical.
- `make_occt_fixtures.py <OCP target> <out_dir>` writes `occt/`: curved solids from the OCCT
  writer. OCCT may create a fixture, never be its oracle — the expectations still come from
  the construction parameters, and the Part 21 text is checked for face count and units.

Run them with `python -I`; they find `p21.py` beside themselves. pytest collects only
`test_*.py`, so nothing here runs as a test.
