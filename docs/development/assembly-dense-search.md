# Optional dense assembly search

`layout.long_search` defaults to false. Fresh UI forms and API requests omitting
the step both use 90 degrees. Finer steps and `-1` (arbitrary Z angles during
long search) remain explicit choices; stored project values are preserved.
Old version-1 assembly records without the new flag remain readable.

The existing assembly child owns the search, under the existing process,
memory, cancellation, publication and 15-minute hard timeout guards. Its
five-minute soft search budget is shared across printer rows. PlateNest's
optional optimizer keeps the bitmap incumbent, tries reordered bitmap angles,
then bounded Sparrow/spyrrow attempts. Original projected triangles are checked
with Shapely for quantities, bed/exclusion boundaries, overlap and spacing.
Only translation and proper in-plane rotation are generated. Explicit existing
input orientation choices are applied before packing, as before.

`POST /api/v1/slice-jobs/{id}/finish-search` uses the existing job visibility and
`library:upload` guards. It requests the best validated candidate at the next
cooperative boundary; it does not cancel or bypass source/publication checks.
The native attempt is bounded at approximately 15 seconds plus setup/cleanup.
Cancellation still discards the job; finish publishes normally.

## Ready-made alternatives

- **PlateNest bitmap + Sparrow/spyrrow (selected):** compatible Python 3.12
  wheels on our Linux target; complementary fast incumbent and irregular search.
  The adapter validates every replacement and retains incumbents for holes or
  excluded regions Sparrow's strip model cannot represent.
- **SVGnest genetic search / libnest2d:** tested SVGnest-style genetic/NFP
  exploration improved synthetic cases but did not beat Sparrow on the real
  fixture, and preparation cost was high. libnest2d's advertised hole/concavity
  limitations do not fit the existing hole-aware baseline.
- **ironnest:** current Python requirements did not match the application's
  Python 3.12 runtime. No parallel packing framework was introduced.

The library dependency is temporarily pinned to an immutable contributor commit
until the companion PlateNest change is released. Switch to that release before
merging if available. spyrrow 0.9.0 is pinned; it uses its bundled Sparrow version,
not necessarily the newest upstream engine. This heuristic does not prove an
optimum, guarantee a speedup, or promise identical layouts across CPU platforms.

## Checks and measured scope

Library tests cover quantities, mixed fills, multi-plate demand, holes, excluded
zones, spacing, free/restricted rotations, finish and cancel. Backend tests cover
old records, child output, the real worker finish signal and endpoint ownership.
UI tests cover the visible opt-in and finish-versus-cancel behavior.

In the local oriented Prop holder fixture on a 180 mm bed, default packing gave
5 copies, finer angles 7 and the optional portfolio 8. Some synthetic concave
fixtures improved; rectangles and hole nesting did not. These are fixture
observations, not a general density guarantee. No production orders are changed
by these tests.

This repository note is the maintainer handoff because the private maintainer
knowledge vault is unavailable in the contributor environment.
